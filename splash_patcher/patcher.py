#!/usr/bin/env python3
"""Make a size/offset-preserving replacement of selected JetBrains IDE PNGs in a JAR.

Unlike conventional ZIP updates, this retains the original central-directory offset,
all local-file offsets, compressed sizes, and every unrelated archive byte. For DEFLATE
entries, it builds an exactly-sized raw DEFLATE stream using uncompressed DEFLATE blocks.
It updates PNG data and the affected ZIP CRC/uncompressed-size metadata only.

Does not edit the input JAR. Requires Python 3.9+ standard library only.
"""

from __future__ import annotations

import os
from pathlib import Path
import struct
import zipfile
import zlib

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def validate_png(png: bytes, dimensions: tuple[int, int]) -> None:
    if not png.startswith(PNG_SIGNATURE) or len(png) < 45:
        raise ValueError('Not a PNG')
    if png[12:16] != b'IHDR' or struct.unpack_from('>II', png, 16) != dimensions:
        raise ValueError(f'Expected a {dimensions[0]}x{dimensions[1]} PNG')
    at = 8
    seen_end = False
    while at + 12 <= len(png):
        length = struct.unpack_from('>I', png, at)[0]
        end = at + 12 + length
        if end > len(png):
            raise ValueError('Truncated PNG chunk')
        kind = png[at+4:at+8]
        calculated = zlib.crc32(png[at+4:end-4]) & 0xffffffff
        if calculated != struct.unpack_from('>I', png, end-4)[0]:
            raise ValueError(f'Invalid PNG {kind!r} checksum')
        if kind == b'IEND':
            if length != 0 or end != len(png):
                raise ValueError('Invalid PNG ending or trailing bytes')
            seen_end = True
            break
        at = end
    if not seen_end:
        raise ValueError('Missing PNG IEND chunk')


def add_padding_chunk(png: bytes, target_size: int) -> bytes:
    excess = target_size - len(png)
    if excess == 0:
        return png
    if excess < 12:
        raise ValueError(
            f'Replacement PNG ({len(png):,} bytes) does not fit in {target_size:,} bytes. '
            'Try a more compressed/smaller image.'
        )
    payload_size = excess - 12
    if payload_size >= 2**31:
        raise ValueError('PNG padding chunk too large')
    chunk_name = b'npAd'  # ancillary, private, safe-to-copy PNG chunk
    data = bytes(payload_size)
    chunk = (struct.pack('>I', payload_size) + chunk_name + data
             + struct.pack('>I', zlib.crc32(chunk_name + data) & 0xffffffff))
    result = png[:-12] + chunk + png[-12:]
    if len(result) != target_size:
        raise AssertionError('PNG size mismatch')
    return result


def exact_size_deflate(png: bytes, compressed_size: int) -> tuple[bytes, bytes]:
    # A DEFLATE 'stored' block is 5 bytes of overhead and up to 65535 data bytes.
    # Choose precisely the number of blocks needed for the existing compressed size.
    blocks = (compressed_size + 65540 - 1) // 65540
    if blocks == 0:
        raise ValueError('Compressed size is zero')
    required_png_size = compressed_size - 5 * blocks
    if required_png_size > 65535 * blocks:
        raise AssertionError('Invalid DEFLATE block count')
    padded = add_padding_chunk(png, required_png_size)
    output = bytearray()
    at = 0
    for index in range(blocks):
        take = min(65535, len(padded) - at)
        # BFINAL=1 on the final block; BTYPE=00 (stored); remaining bits zero.
        output.append(1 if index == blocks - 1 else 0)
        output.extend(struct.pack('<HH', take, 0xffff ^ take))
        output.extend(padded[at:at + take])
        at += take
    if at != len(padded) or len(output) != compressed_size:
        raise AssertionError('DEFLATE length mismatch')
    compressed = bytes(output)
    if zlib.decompress(compressed, -15) != padded:
        raise AssertionError('DEFLATE verification failed')
    return compressed, padded


def locate_central_directory(data: bytearray, entries: list[zipfile.ZipInfo], start: int) -> dict[str, int]:
    offsets = {}
    offset = start
    for item in entries:
        if data[offset:offset + 4] != b'PK\x01\x02':
            raise ValueError('Unexpected central-directory format')
        name_len, extra_len, comment_len = struct.unpack_from('<HHH', data, offset + 28)
        filename = data[offset+46:offset+46+name_len]
        encoding = 'utf-8' if item.flag_bits & 0x800 else 'cp437'
        if filename.decode(encoding) != item.filename:
            raise ValueError('Central-directory entry name/order mismatch')
        offsets[item.filename] = offset
        offset += 46 + name_len + extra_len + comment_len
    return offsets


def patch_entry(data: bytearray, info: zipfile.ZipInfo, cd_pos: int, png: bytes, cd_start: int) -> None:
    if info.compress_type not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
        raise ValueError(f'{info.filename}: unsupported ZIP compression method {info.compress_type}')
    if max(info.compress_size, info.file_size, info.header_offset) >= 0xffffffff:
        raise ValueError(f'{info.filename}: ZIP64 not supported')
    if info.flag_bits & 0x1:
        raise ValueError(f'{info.filename}: encrypted ZIP entry not supported')

    local = info.header_offset
    if data[local:local+4] != b'PK\x03\x04':
        raise ValueError(f'{info.filename}: invalid local ZIP header')
    local_flags, method = struct.unpack_from('<HH', data, local + 6)
    if local_flags != info.flag_bits or method != info.compress_type:
        raise ValueError(f'{info.filename}: local/central ZIP metadata mismatch')
    name_len, extra_len = struct.unpack_from('<HH', data, local + 26)
    start = local + 30 + name_len + extra_len
    extra_at = local + 30 + name_len
    while extra_at < start:
        if extra_at + 4 > start:
            raise ValueError('Truncated ZIP extra field')
        field_id, field_size = struct.unpack_from('<HH', data, extra_at)
        if field_id == 1:
            raise ValueError(f'{info.filename}: ZIP64 not supported')
        extra_at += 4 + field_size
        if extra_at > start:
            raise ValueError('Truncated ZIP extra field')
    raw_name = data[local+30:local+30+name_len]
    encoding = 'utf-8' if info.flag_bits & 0x800 else 'cp437'
    if raw_name.decode(encoding) != info.filename:
        raise ValueError(f'{info.filename}: local ZIP name mismatch')
    if struct.unpack_from('<III', data, cd_pos + 16) != (info.CRC, info.compress_size, info.file_size):
        raise ValueError(f'{info.filename}: central ZIP metadata mismatch')

    if info.compress_type == zipfile.ZIP_DEFLATED:
        compressed, padded = exact_size_deflate(png, info.compress_size)
    else:
        padded = add_padding_chunk(png, info.file_size)
        compressed = padded
    crc = zlib.crc32(padded) & 0xffffffff
    end = start + info.compress_size
    if end > cd_start or end > len(data):
        raise ValueError(f'{info.filename}: ZIP data outside archive')
    if len(compressed) != info.compress_size:
        raise AssertionError('Changed compressed ZIP member size')

    # Bit 3 indicates CRC/size metadata in a descriptor after compressed data.
    if info.flag_bits & 0x8:
        position = end
        if data[position:position+4] == b'PK\x07\x08':
            position += 4
        if struct.unpack_from('<III', data, position) != (info.CRC, info.compress_size, info.file_size):
            raise ValueError(f'{info.filename}: ZIP data descriptor mismatch')
        struct.pack_into('<III', data, position, crc, info.compress_size, len(padded))
        # Preserve zero placeholders, but update any populated local fields.
        old_fields = struct.unpack_from('<III', data, local + 14)
        expected_fields = (info.CRC, info.compress_size, info.file_size)
        new_fields = (crc, info.compress_size, len(padded))
        if any(old not in (0, expected) for old, expected in zip(old_fields, expected_fields)):
            raise ValueError(f'{info.filename}: invalid descriptor local metadata')
        struct.pack_into('<III', data, local + 14,
                         *(new if old else 0 for old, new in zip(old_fields, new_fields)))
    else:
        if struct.unpack_from('<III', data, local + 14) != (info.CRC, info.compress_size, info.file_size):
            raise ValueError(f'{info.filename}: local ZIP metadata mismatch')
        struct.pack_into('<III', data, local + 14, crc, info.compress_size, len(padded))
    data[start:end] = compressed
    struct.pack_into('<III', data, cd_pos + 16, crc, info.compress_size, len(padded))


def patch_jar(source: Path, output: Path, images: dict[str, bytes]) -> None:
    """Build and verify a copy. Never repack the archive or overwrite a source."""
    import io
    from PIL import Image

    if source.resolve() == output.resolve() or output.exists():
        raise ValueError('Output must be a new file separate from the source')
    if not images:
        raise ValueError('No replacements selected')
    data = bytearray(source.read_bytes())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len({e.filename for e in entries}) != len(entries):
            raise ValueError('Duplicate ZIP member names are unsupported')
        if any(e.filename.upper().startswith('META-INF/') and
               e.filename.upper().endswith(('.SF', '.RSA', '.DSA', '.EC')) for e in entries):
            raise ValueError('Signed JARs cannot be patched safely')
        if archive.testzip():
            raise ValueError('Source JAR failed CRC verification')
        offsets = locate_central_directory(data, entries, archive.start_dir)
        dimensions = {}
        for name, png in images.items():
            if not name.lower().endswith('.png'):
                raise ValueError('Only PNG resources may be replaced')
            info = archive.getinfo(name)
            with Image.open(io.BytesIO(archive.read(name))) as original:
                dimensions[name] = original.size
            validate_png(png, dimensions[name])
            with Image.open(io.BytesIO(png)) as replacement:
                replacement.load()
            patch_entry(data, info, offsets[name], png, archive.start_dir)
        with zipfile.ZipFile(io.BytesIO(data)) as result:
            if result.testzip():
                raise ValueError('Patched archive failed CRC verification')
            if result.start_dir != archive.start_dir or len(data) != source.stat().st_size:
                raise ValueError('Archive layout changed')
            for before, after in zip(entries, result.infolist(), strict=True):
                if (before.filename, before.header_offset, before.compress_size) != (
                        after.filename, after.header_offset, after.compress_size):
                    raise ValueError('Archive member layout changed')
                if before.filename not in images:
                    if archive.read(before) != result.read(after):
                        raise ValueError('Unselected resource changed')
                else:
                    actual = result.read(after)
                    validate_png(actual, dimensions[before.filename])
                    with Image.open(io.BytesIO(actual)) as im:
                        im.load()
                        with Image.open(io.BytesIO(images[before.filename])) as expected:
                            if im.convert('RGBA').tobytes() != expected.convert('RGBA').tobytes():
                                raise ValueError('Replacement pixels changed')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
