"""IDE metadata and bounded, read-only PNG discovery."""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
import zipfile

from PIL import Image, ImageOps

MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 32_000_000


@dataclass(frozen=True)
class Installation:
    root: Path
    name: str
    build: str
    data_directory: str
    launchers: tuple[str, ...]

    @classmethod
    def load(cls, root: Path) -> Installation:
        root = root.resolve(strict=True)
        data = json.loads((root / 'product-info.json').read_text(encoding='utf-8-sig'))
        directory = data.get('dataDirectoryName', '')
        if not directory or not re.fullmatch(r'[\w. -]+', directory) or directory in ('.', '..'):
            raise ValueError('Invalid or missing product dataDirectoryName')
        if not data.get('buildNumber') or not (root / 'lib').is_dir():
            raise ValueError('Select an IDE installation containing product-info.json and lib')
        launchers = tuple(Path(x.get('launcherPath', '').replace('\\', '/')).name.lower()
                          for x in data.get('launch', []) if x.get('launcherPath'))
        return cls(root, data.get('name', root.name), str(data['buildNumber']), directory, launchers)


@dataclass(frozen=True)
class Resource:
    jar: str
    name: str
    width: int
    height: int
    compressed_size: int

    @property
    def pair_key(self) -> str:
        return re.sub(r'@2x(?=\.png$)', '', self.name, flags=re.I)


def read_png(root: Path, resource: Resource) -> bytes:
    path = (root / resource.jar).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError('JAR escapes the installation')
    with zipfile.ZipFile(path) as jar:
        item = jar.getinfo(resource.name)
        if item.file_size > MAX_PNG_BYTES:
            raise ValueError('Image exceeds the 64 MiB resource limit')
        return jar.read(item)


def scan(ide: Installation, all_pngs: bool = False, progress=lambda _: None):
    found, warnings = [], []
    jars = sorted((ide.root / 'lib').rglob('*.jar'))
    for index, jar_path in enumerate(jars, 1):
        progress(f'Scanning {index}/{len(jars)}: {jar_path.name}')
        if not jar_path.resolve().is_relative_to(ide.root):
            warnings.append(f'Skipped external symlink: {jar_path}')
            continue
        try:
            with zipfile.ZipFile(jar_path) as jar:
                for item in jar.infolist():
                    if not item.filename.lower().endswith('.png') or item.file_size > MAX_PNG_BYTES:
                        continue
                    if not all_pngs and not re.search(r'splash|logo|banner|artwork', item.filename, re.I):
                        continue
                    try:
                        with Image.open(io.BytesIO(jar.read(item))) as im:
                            if im.width * im.height > MAX_PIXELS:
                                continue
                            im.verify()
                            found.append(Resource(jar_path.relative_to(ide.root).as_posix(),
                                                  item.filename, im.width, im.height, item.compress_size))
                    except (ValueError, OSError, Image.DecompressionBombError) as exc:
                        warnings.append(f'{jar_path.name}: {item.filename}: {exc}')
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            warnings.append(f'{jar_path.name}: {exc}')
    return found, warnings, len(jars)


def make_replacement(path: Path, size: tuple[int, int], mode: str) -> bytes:
    with Image.open(path) as source:
        if source.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('Choose a PNG, JPEG, or WebP image')
        if source.width * source.height > MAX_PIXELS:
            raise ValueError('Replacement exceeds 32 megapixels')
        source = ImageOps.exif_transpose(source).convert('RGBA')
        if mode == 'Crop':
            result = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
        elif mode == 'Fit':
            scaled = ImageOps.contain(source, size, method=Image.Resampling.LANCZOS)
            result = Image.new('RGBA', size, (0, 0, 0, 255))
            result.alpha_composite(scaled, ((size[0]-scaled.width)//2, (size[1]-scaled.height)//2))
        else:
            raise ValueError('Unknown resize mode')
        if result.getextrema()[3] == (255, 255):
            result = result.convert('RGB')
        out = io.BytesIO()
        result.save(out, format='PNG', optimize=True)
        return out.getvalue()
