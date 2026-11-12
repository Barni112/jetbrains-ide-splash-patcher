# JetBrains IDE Splash Patcher

A small Python desktop app for browsing JetBrains IDE artwork, previewing custom images, and applying or restoring replacements with original JAR backups.

The first release targets **Windows 10/11, Python 3.11+, Tkinter, Pillow, and psutil**. Resource names are discovered from the selected installation; PyCharm, IntelliJ IDEA, CLion, WebStorm, and other JetBrains IDE names are not hardcoded. Compatibility depends on the archive format and should be tested on each IDE/build.

## Run on Windows

Install Python with Tcl/Tk support (included in the standard Windows installer). In PowerShell:

```powershell
 git clone --branch feat/desktop-splash-manager https://github.com/Barni112/jetbrains-ide-splash-patcher.git
 cd jetbrains-ide-splash-patcher
 py -3 -m venv .venv
 .\.venv\Scripts\python.exe -m pip install -e .
 .\.venv\Scripts\python.exe -m splash_patcher
```

No PowerShell activation script is needed. After installation, `start.cmd` also opens the app using this virtual environment. Run the GUI normally; it requests Windows UAC only when the installation directories require it. There is no packaged executable in this release.

## Replace artwork

1. **Browse** to the IDE directory containing `product-info.json` and `lib`. The app reads the actual build, independent of the installation folder's name.
2. Choose a persistent **Backup workspace**. The default is `splash-workspace` under your working directory. Keep this directory to retain originals and recovery records.
3. Scan likely splash/logo/banner/artwork PNGs, or enable **Scan all PNGs** for other resources. Filter by resource or JAR path.
4. Select a standard/`@2x` pair to replace both sizes, or expand it and select one image. Previews show the currently installed artwork and queued replacement. For a pair, the preview shows its smaller member; select either child to inspect that size individually.
5. Choose a PNG, JPEG, or WebP. **Crop** fills the original dimensions without stretching; **Fit** uses black margins. Pick the resize mode before choosing the image. Queue images across as many JARs as needed.
6. Close the IDE completely, then **Review & apply**. The review lists exact archive and resource names. Before the first patch of each JAR, identify its original (see below).
7. The app creates immutable original backups, prepares and verifies all patched copies, then installs them. It renames the splash cache and preserves it as a backup. Start the IDE normally.

### Already customized your PyCharm installation?

When asked whether installed JARs are untouched originals, choose **No** and select your existing unmodified JAR backup (including a `.bak` file). Imported backups must have the same archive layout and unchanged non-PNG resources. There is no reliable general way to prove that a supplied JAR is an official original; the app records your explicit choice.

Choose **Yes** only when the installed JARs are the originals you want to restore later. Existing original backups are never replaced on later runs. Do not use an updated IDE's JAR as a backup for an older build.

### Restore and recovery

- **Restore originals** verifies the recorded SHA-256 hashes and restores all tracked JARs for the selected installation/workspace. Original backups remain available.
- **Recover interrupted operation** returns all JARs to their pre-operation state. Use this after a crash or to discard an operation left pending by a canceled UAC prompt. It refuses to overwrite a JAR whose contents have changed unexpectedly.
- Multi-JAR operations are not one filesystem-wide atomic change. Each JAR is replaced atomically; failures trigger rollback of completed replacements. A durable pending record supports recovery after process termination. Keep the IDE closed until completion or recovery.
- A changed build or unexpected installed JAR hash blocks patch/restore. After an IDE update, use a new workspace for that build and retain the old workspace. Never restore an old build over a new one.
- **Clear splash cache** retries cache invalidation without changing any JARs. The default location is `%LOCALAPPDATA%\JetBrains\<dataDirectoryName>\splash` for the user running the GUI. If your IDE uses a custom system directory, select its actual folder named `splash`. Cache work runs without elevation, even when installation required UAC.

## How patching works

This app adapts the offset-preserving Python patcher used successfully in the earlier PyCharm PowerShell workflow. Conventional ZIP rebuilding moved archive offsets and caused a JetBrains class-loader failure in that workflow.

The patcher preserves total JAR size, central-directory offset, entry positions, compressed lengths, archive comments, and unrelated bytes. It replaces selected PNG payloads and updates their ZIP CRC and uncompressed-size metadata, including supported data descriptors. Stored entries use a private PNG padding chunk. DEFLATE entries use exactly sized stored DEFLATE blocks containing a padded PNG. The output is checked for CRC validity, unchanged member layout, unchanged unrelated resources, image dimensions, and decoded pixels before installation.

**A replacement must fit the existing compressed byte budget.** Automatic resizing and PNG optimization help, but detailed images may still be too large. The app refuses the operation instead of repacking the JAR or silently reducing image quality. Choose simpler artwork or an externally optimized PNG. Tiny gaps of 1–11 bytes cannot accommodate the padding chunk and can also be refused.

Signed JARs, encrypted entries, unsupported compression methods, duplicate entry names, and unsupported ZIP64 target layouts are rejected. This is an unofficial modification; IDE updates may replace customized archives. macOS app bundles/signatures and Linux-specific installation/cache conventions are outside this release's supported UI workflow.

## Workspace contents

Each installation is identified by its full path. Its workspace contains:

- `manifest.json`: build, JAR paths, original and current SHA-256 hashes.
- `originals/`: immutable copies of original JARs (unique names, mapped in the manifest).
- `operations/<id>/`: pre-operation and staged JARs, replacement PNGs, entry mappings, plan and helper result.
- `pending.json`: present while an operation needs completion/recovery.

Operations are retained for diagnosis and recovery. They can use substantial disk space for large JARs. Do not delete the workspace while an operation is pending. Queued edits are held in memory until Apply; closing the app discards an unapplied queue after confirmation.

## Development and validation

```powershell
 .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On Linux, install Tk and use `xvfb-run -a python -m unittest discover -s tests -v` to exercise the real widgets. Without a display, only the GUI test is skipped. CI covers Windows and Linux on Python 3.11 and 3.13.

Tests cover stored/deflated archives with and without data descriptors, exact layout preservation, image-size failures, signed/duplicate archive rejection, discovery, crop/fit, multi-JAR rollback, crash recovery, original import, tampering, restore, helper subprocess execution, cache rename, and the Tk image-selection/preview queue.

**Manual Windows release check:** scan an actual IDE, import its known original if already customized, apply both DPI variants, approve UAC if needed, confirm the splash after cache invalidation, then restore and relaunch. Automated fixtures do not establish compatibility with every JetBrains build, and UAC approval itself is not exercised by unit tests.

## Modules

- `discovery.py`: product metadata, resource scan, image conversion.
- `patcher.py`: offset-preserving archive transformation and validation.
- `transactions.py`: backups, hashes, staging, install, rollback, recovery, cache handling.
- `installer.py` / `helper.py`: short-lived installation process and Windows elevation.
- `gui.py`: Tk browser, preview, queue, review, apply and restore.

Windows elevation uses [`ShellExecuteExW`](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfow) with the `runas` verb and a waited process handle. The GUI itself stays unelevated.
