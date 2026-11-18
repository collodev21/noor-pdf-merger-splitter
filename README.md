# Noor PDF Merger/Splitter

A lightweight offline Windows utility to merge PDFs in a user-controlled file order or split one PDF into multiple complete page groups. It has a Tkinter desktop interface and a command-line mode. The source PDFs are not edited in place.

Day 8's [PDF Page Reorganizer](https://github.com/collodev21/noor-pdf-page-reorganizer) already handles reordering, rotating and extracting pages from one document. This project instead focuses on ordered assembly across multiple input files and partitioning every page of one source into separate output files. It does not add page rearrangement or extract-only workflows.

## Features

- Add two or more PDFs to a merge queue, move files up or down, and merge them in that order.
- Split one PDF into two or more contiguous page groups, for example `1-3;4-6;7`.
- Require split groups to cover every source page exactly once. The parser rejects empty, overlapping, descending, out-of-order, incomplete and out-of-range ranges.
- Preserve page-level PDF content, searchable text and mixed page dimensions; verify page counts before publishing output.
- Protect source files and refuse output collisions by default. A merge can replace an existing output only when explicitly confirmed in the GUI or requested with `--overwrite` in the CLI. Split parts are staged in a temporary sibling folder and published as a new directory; that destination directory must not already exist.
- Process files locally with no uploads, accounts, telemetry or network calls.
- Enforce limits of 250 MiB per PDF, 500 MiB combined for a merge, 50 merge inputs, 2,000 pages per input and 5,000 pages combined.

## Alternatives and scope

PDFsam Basic is an established open-source offline PDF application with a broader set of merge, split, extract, mix and rotate tools. Its merge flow also supports page ranges and options for bookmarks and AcroForms. PDF Arranger provides an interactive page-oriented merge/split/rotate/crop/rearrange workflow. Noor's tool is intentionally narrower: it offers a simple ordered multi-file queue and a strict complete-partition split operation with collision-safe outputs. These workflows are not claimed to be unique or to replace PDFsam or PDF Arranger. [PDFsam Basic](https://pdfsam.org/pdfsam-basic/) · [PDF Arranger](https://github.com/pdfarranger/pdfarranger).

## Windows requirements and installation

- Windows 10/11, Python 3.10 or newer, and a Python installation with Tcl/Tk support for the desktop interface.
- The `pypdf` dependency in `requirements.txt`.

In PowerShell from the project folder:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python .\src\noor_pdf_merger_splitter.py
```

Running the script without arguments opens the GUI. Choose input PDFs in order, then use **Move up** or **Move down** to correct the merge sequence. In the Split PDF tab, enter semicolon-separated page groups and choose a new output folder.

## Command-line usage

Merge in the order listed on the command line:

```powershell
python .\src\noor_pdf_merger_splitter.py merge `
  .\cover.pdf .\application.pdf .\receipt.pdf `
  --output .\combined.pdf
```

If the destination exists, the command refuses to replace it unless you pass `--overwrite`.

Split a 9-page PDF into three complete outputs:

```powershell
python .\src\noor_pdf_merger_splitter.py split .\document.pdf `
  --groups "1-4;5-8;9" `
  --output-dir .\document_parts
```

The group boundaries use one-based, inclusive page numbers. Each group is a single page or an ascending range. Separate groups with semicolons. The groups must start at page 1, have no gaps or overlaps, and end at the last page. For example, `1-2;3-4;5-6` partitions a six-page PDF into three files; `1;2;3;4;5;6` makes one file per page. There must be at least two output groups, and the output folder must be new.

CLI help is available with:

```powershell
python .\src\noor_pdf_merger_splitter.py --help
python .\src\noor_pdf_merger_splitter.py merge --help
python .\src\noor_pdf_merger_splitter.py split --help
```

## PDF behavior and safety limits

The utility writes new PDFs from page objects rather than rasterizing them. It checks input PDF structure, rejects empty, corrupt and password-protected files, validates page counts, protects source paths and verifies each generated output before publishing it. Merge output is published from a same-folder temporary file without replacing an existing destination by default. Split output parts are built and validated in a temporary directory before the directory is moved into place.

A PDF rewrite can change or discard document-level features. Bookmarks, named destinations, forms, attachments, links, metadata, tagged accessibility structure and other non-page information are not guaranteed to survive. Rewriting invalidates existing digital signatures. This is not an archival/PDF-A validator, OCR tool, PDF editor or document-security scanner. Review outputs before relying on them, especially when handling signed or official documents. Password-protected PDFs are not supported.

## Tests and build status

Install development dependencies and run:

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q src tests
python -m unittest discover -s tests -v
```

Tests use synthetic PDFs and cover input ordering, searchable text, mixed page dimensions, complete split partitions, invalid and overlapping ranges, password-protected and corrupt files, Unicode paths, limits, source preservation, collision protection, progress callbacks and cleanup after simulated write failures. GitHub Actions is configured for Windows and Ubuntu with Python 3.10 and 3.12. The suite tests the Python processing core; it does not launch the Tkinter GUI or use physical devices.

No Windows executable, installer or release is included or claimed.

## License

MIT; see [LICENSE](LICENSE).
