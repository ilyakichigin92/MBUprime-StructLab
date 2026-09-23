# Windows quick start

[Download the latest Windows release](https://github.com/ilyakichigin92/MBUprime-StructLab/releases/latest) · [Русский](quick-start-ru.md) · [User guide](user-guide.md)

## Download and launch

1. Under **Assets**, download `MBUprime-StructLab-<version>-windows-x64-unsigned.zip` and its `.zip.sha256` sidecar. Substitute the version shown on that release page. **Source code (zip)** and **Source code (tar.gz)** contain source, not the runnable application.
2. Compare the ZIP's SHA-256 with the sidecar before extracting. In PowerShell, `Get-FileHash -Algorithm SHA256 "C:\Downloads\your-downloaded-file.zip"` prints the hash; replace the example path with your downloaded ZIP. Hashes check file contents, not publisher identity.
3. Extract the **complete ZIP** into a short ASCII-only path such as `C:\MBUprimeStructLab`. Keep `MBUprime StructLab.exe` beside its `_internal` directory. Do not launch from inside the ZIP or move the executable alone.
4. Open `MBUprime StructLab.exe`. Python and all four engines are included; you do not need to install Python. The public application opens in English. Choose **Русский** in the **Language** selector to switch to Russian.

The executable is unsigned. Windows security controls may ask for confirmation or block it under local policy; obtain it from the project release page and follow your organization's policy. A resolved application path containing non-ASCII characters can prevent Primer3 from loading. Use the ASCII-only folder above, including its parent folders.

Windows 10 22H2 build 19045 has executed host evidence. Clean Windows 11 qualification is pending; Ubuntu and macOS routes remain unverified. See [platform evidence](platforms.md).

## Run the supplied synthetic example

1. Choose **Bulk / multiplex** and replace the input with these two lines from [bulk.txt](../examples/small-panel/bulk.txt):

   ```text
   F_demo GCGCAAAAGCGC
   R_demo GCGCTTTTGCGC
   ```

2. Keep the default reaction conditions and select **Analyze**. Wait for analysis to complete before inspecting the new results.
3. Inspect **Melting temperatures**, **Flagged structures**, **Matrix** and **Structures**. Select a structure to see its detailed geometry or diagram.
4. Select **Export**, choose a TSV report and save it to a writable folder. Open the saved file in a text editor or spreadsheet. Field names stay in English in both interface languages.

The [worked example](../examples/small-panel/README.md) records its actual historical version and settings; it is a synthetic demonstration, not a validated PCR assay. Predictions support screening and do not establish experimental assay performance. See [methods and limits](methods.md).

On a small screen, each of the two columns has its own outer scrollbars; use them to reach lower controls. Drag the divider to adjust column widths. If startup fails, use the [self-test instructions](user-guide.md#start-the-desktop-application) and report the version, Windows version and sanitized diagnostics in [Issues](https://github.com/ilyakichigin92/MBUprime-StructLab/issues).
