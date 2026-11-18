"""Noor PDF Merger/Splitter: offline ordered merging and page-group splitting."""
from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from pypdf import PdfReader, PdfWriter

APP_NAME = "Noor PDF Merger/Splitter"
MAX_FILE_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILES = 50
MAX_PAGES_PER_FILE = 2_000
MAX_TOTAL_PAGES = 5_000
ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class PDFInfo:
    path: Path
    pages: int
    size_bytes: int


@dataclass(frozen=True)
class MergeResult:
    output_path: Path
    input_files: int
    pages: int


@dataclass(frozen=True)
class SplitResult:
    output_dir: Path
    files: tuple[Path, ...]
    page_groups: tuple[tuple[int, int], ...]


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _lexical_absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def inspect_pdf(source: str | os.PathLike[str]) -> PDFInfo:
    """Validate one unencrypted PDF and return its page count and file size."""
    try:
        path = Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError(f"Cannot find or resolve PDF input: {source}") from exc
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError(f"Input must be an existing PDF file: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Cannot read PDF size: {path}") from exc
    if size == 0:
        raise ValueError(f"PDF input is empty: {path}")
    if size > MAX_FILE_BYTES:
        raise ValueError(f"PDF exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MiB per-file limit: {path.name}")
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=False)
            if reader.is_encrypted:
                raise ValueError(f"Password-protected PDFs are not supported: {path.name}")
            pages = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Cannot read PDF '{path.name}': {exc}") from exc
    if pages < 1:
        raise ValueError(f"PDF contains no pages: {path.name}")
    if pages > MAX_PAGES_PER_FILE:
        raise ValueError(f"PDF exceeds the {MAX_PAGES_PER_FILE}-page per-file limit: {path.name}")
    return PDFInfo(path=path, pages=pages, size_bytes=size)


def _validate_source_list(sources: Sequence[str | os.PathLike[str]], *, minimum: int) -> tuple[PDFInfo, ...]:
    if isinstance(sources, (str, bytes, os.PathLike)):
        raise ValueError("Pass PDF inputs as a list of paths, not a single path")
    try:
        items = list(sources)
    except TypeError as exc:
        raise ValueError("PDF inputs must be a list of paths") from exc
    if len(items) < minimum:
        raise ValueError(f"Select at least {minimum} PDF files")
    if len(items) > MAX_FILES:
        raise ValueError(f"Select no more than {MAX_FILES} PDF files")
    infos = tuple(inspect_pdf(item) for item in items)
    for index, info in enumerate(infos):
        if any(_same_file(info.path, earlier.path) for earlier in infos[:index]):
            raise ValueError(f"The same PDF was selected more than once: {info.path.name}")
    total_size = sum(info.size_bytes for info in infos)
    total_pages = sum(info.pages for info in infos)
    if total_size > MAX_TOTAL_BYTES:
        raise ValueError(f"Selected PDFs exceed the combined {MAX_TOTAL_BYTES // (1024 * 1024)} MiB limit")
    if total_pages > MAX_TOTAL_PAGES:
        raise ValueError(f"Selected PDFs exceed the combined {MAX_TOTAL_PAGES}-page limit")
    return infos


def _validate_output_file(output: str | os.PathLike[str], inputs: Sequence[Path], overwrite: bool) -> Path:
    if not isinstance(overwrite, bool):
        raise ValueError("Overwrite must be true or false")
    try:
        target = _lexical_absolute(output)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("Enter a valid output path") from exc
    if target.suffix.lower() != ".pdf":
        raise ValueError("Output file must have a .pdf extension")
    if target.is_symlink():
        raise ValueError("Refusing to write to a symbolic-link output path")
    if any(_same_file(target, source) for source in inputs):
        raise ValueError("Output path points to one of the input PDFs; choose a new filename")
    if (target.exists() or target.is_symlink()) and not overwrite:
        raise FileExistsError(f"Output already exists: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create output folder: {target.parent}") from exc
    if not target.parent.is_dir():
        raise ValueError(f"Output parent is not a folder: {target.parent}")
    return target


def _count_pages(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=False)
            if reader.is_encrypted:
                raise ValueError("Generated PDF unexpectedly requires a password")
            return len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Generated PDF failed validation: {exc}") from exc


def _publish_file(temporary: Path, target: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(temporary, target)
        return
    if os.name == "nt":
        try:
            # On Windows, rename fails rather than replacing an existing file.
            os.rename(temporary, target)
        except FileExistsError as exc:
            raise FileExistsError(f"Output appeared while processing: {target}") from exc
        except OSError as exc:
            raise OSError(f"Cannot atomically publish output: {exc}") from exc
        return
    try:
        # A hard link publishes the completed file atomically without replacing
        # a destination that may have appeared after the initial collision check.
        os.link(temporary, target)
    except FileExistsError as exc:
        raise FileExistsError(f"Output appeared while processing: {target}") from exc
    except OSError as exc:
        raise OSError(f"Cannot atomically publish output on this filesystem: {exc}") from exc
    temporary.unlink(missing_ok=True)


def merge_pdfs(sources: Sequence[str | os.PathLike[str]], output: str | os.PathLike[str], *,
               overwrite: bool = False, progress: ProgressCallback | None = None) -> MergeResult:
    """Merge two or more PDFs in the supplied order, leaving inputs untouched."""
    infos = _validate_source_list(sources, minimum=2)
    target = _validate_output_file(output, [info.path for info in infos], overwrite)
    total_pages = sum(info.pages for info in infos)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.stem}.tmp-", suffix=".pdf", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    writer = PdfWriter()
    try:
        for index, info in enumerate(infos, start=1):
            try:
                writer.append(str(info.path))
            except Exception as exc:
                raise ValueError(f"Failed to add '{info.path.name}' to the merged PDF: {exc}") from exc
            if progress is not None:
                progress(index, len(infos))
        writer.add_metadata({"/Producer": APP_NAME, "/Title": target.stem})
        writer.write(str(temporary))
        writer.close()
        actual_pages = _count_pages(temporary)
        if actual_pages != total_pages:
            raise ValueError(f"Merged output has {actual_pages} pages; expected {total_pages}")
        _publish_file(temporary, target, overwrite)
        return MergeResult(output_path=target, input_files=len(infos), pages=actual_pages)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        temporary.unlink(missing_ok=True)


def parse_split_groups(specification: str, page_count: int) -> tuple[tuple[int, int], ...]:
    """Parse semicolon-separated, one-based contiguous ranges partitioning all pages.

    Example: ``1-3;4-6;7`` creates three outputs and assigns every input page once.
    """
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise ValueError("PDF page count must be a positive integer")
    if not isinstance(specification, str) or not specification.strip():
        raise ValueError("Enter page groups such as 1-3;4-6;7")
    tokens = [part.strip() for part in specification.split(";")]
    if any(not token for token in tokens):
        raise ValueError("Page groups cannot be empty; separate groups with semicolons")
    if len(tokens) < 2:
        raise ValueError("A split needs at least two output groups")
    groups: list[tuple[int, int]] = []
    expected_start = 1
    for token in tokens:
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", token)
        if not match:
            raise ValueError(f"Invalid page group '{token}'; use a page number or ascending range such as 4-8")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError(f"Page group must be an ascending one-based range: {token}")
        if start != expected_start:
            if start < expected_start:
                raise ValueError(f"Page groups overlap or repeat a page near page {start}")
            raise ValueError(f"Page groups must include every page; expected the next group to start at page {expected_start}")
        if end > page_count:
            raise ValueError(f"Page {end} is outside this {page_count}-page PDF")
        groups.append((start, end))
        expected_start = end + 1
    if expected_start != page_count + 1:
        raise ValueError(f"Page groups omit page(s) starting at {expected_start}; include all {page_count} pages")
    return tuple(groups)


def _write_split_part(source: Path, stage: Path, index: int, group: tuple[int, int]) -> Path:
    start, end = group
    filename = f"part_{index:02d}_pages_{start:04d}-{end:04d}.pdf"
    target = stage / filename
    writer = PdfWriter()
    try:
        writer.append(str(source), pages=(start - 1, end), import_outline=False)
        writer.add_metadata({"/Producer": APP_NAME, "/Title": filename[:-4]})
        writer.write(str(target))
        writer.close()
    finally:
        try:
            writer.close()
        except Exception:
            pass
    expected_pages = end - start + 1
    actual_pages = _count_pages(target)
    if actual_pages != expected_pages:
        raise ValueError(f"Split output {filename} has {actual_pages} pages; expected {expected_pages}")
    return target


def split_pdf(source: str | os.PathLike[str], output_dir: str | os.PathLike[str], groups: str, *,
              progress: ProgressCallback | None = None) -> SplitResult:
    """Split one PDF into a new directory of complete, contiguous page groups.

    The destination directory must not already exist. All parts are staged in a
    sibling temporary folder, validated, and then published as one directory.
    """
    info = inspect_pdf(source)
    parsed_groups = parse_split_groups(groups, info.pages)
    try:
        target = _lexical_absolute(output_dir)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("Enter a valid output folder") from exc
    if target.is_symlink() or target.exists():
        raise FileExistsError(f"Output folder already exists; choose a new folder: {target}")
    if _same_file(target, info.path):
        raise ValueError("Output folder cannot be the input PDF")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create output parent folder: {target.parent}") from exc
    if not target.parent.is_dir():
        raise ValueError(f"Output parent is not a folder: {target.parent}")
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    output_files: list[Path] = []
    try:
        for index, group in enumerate(parsed_groups, start=1):
            output_files.append(_write_split_part(info.path, stage, index, group))
            if progress is not None:
                progress(index, len(parsed_groups))
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Output folder appeared while processing: {target}")
        os.rename(stage, target)
        stage = Path()
        final_files = tuple(target / path.name for path in output_files)
        return SplitResult(output_dir=target, files=final_files, page_groups=parsed_groups)
    finally:
        if stage != Path() and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _progress_message(current: int, total: int) -> None:
    print(f"Progress: {current}/{total}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noor-pdf-merger-splitter", description="Merge PDFs in a chosen order or split a PDF into page groups (offline).")
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge", help="merge two or more PDFs in the listed order")
    merge.add_argument("inputs", nargs="+", help="input PDFs in desired output order")
    merge.add_argument("-o", "--output", required=True, help="new output .pdf path")
    merge.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    split = commands.add_parser("split", help="split all pages into contiguous, complete groups")
    split.add_argument("input", help="input PDF")
    split.add_argument("--groups", required=True, help="semicolon-separated page groups, e.g. 1-3;4-6;7")
    split.add_argument("--output-dir", required=True, help="new output folder; it must not already exist")
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "merge":
            result = merge_pdfs(args.inputs, args.output, overwrite=args.overwrite, progress=_progress_message)
            print(f"Merged {result.input_files} PDF files into {result.output_path} ({result.pages} page(s)).")
        else:
            result = split_pdf(args.input, args.output_dir, args.groups, progress=_progress_message)
            print(f"Created {len(result.files)} PDF part(s) in {result.output_dir}.")
            for path, group in zip(result.files, result.page_groups):
                print(f"  pages {group[0]}-{group[1]}: {path.name}")
        return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 2


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    events: queue.Queue[tuple[str, object]] = queue.Queue()

    class App:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(APP_NAME)
            self.root.geometry("760x540")
            self.root.minsize(650, 460)
            self.busy = False
            self.inputs: list[str] = []
            self.merge_output = tk.StringVar()
            self.split_source = tk.StringVar()
            self.split_groups = tk.StringVar(value="1-1")
            self.split_output = tk.StringVar()
            self.status = tk.StringVar(value="Choose files to begin. Everything is processed locally.")
            self.progress_value = tk.DoubleVar(value=0)
            self.progress_total = 1
            self._build(ttk, filedialog)
            self.root.after(100, self._poll)

        def _build(self, ttk_module, dialogs) -> None:
            outer = ttk_module.Frame(self.root, padding=14)
            outer.pack(fill="both", expand=True)
            notebook = ttk_module.Notebook(outer)
            notebook.pack(fill="both", expand=True)

            merge_tab = ttk_module.Frame(notebook, padding=12)
            notebook.add(merge_tab, text="Merge PDFs")
            merge_tab.columnconfigure(0, weight=1)
            ttk_module.Label(merge_tab, text="Add PDFs, then set their order with Move up/down.").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
            self.listbox = tk.Listbox(merge_tab, selectmode="extended", height=11)
            self.listbox.grid(row=1, column=0, rowspan=4, sticky="nsew", padx=(0, 8))
            merge_tab.rowconfigure(1, weight=1)
            ttk_module.Button(merge_tab, text="Add PDFs…", command=self._add_files).grid(row=1, column=1, sticky="ew", pady=2)
            ttk_module.Button(merge_tab, text="Remove selected", command=self._remove_files).grid(row=2, column=1, sticky="ew", pady=2)
            ttk_module.Button(merge_tab, text="Move up", command=lambda: self._move(-1)).grid(row=3, column=1, sticky="ew", pady=2)
            ttk_module.Button(merge_tab, text="Move down", command=lambda: self._move(1)).grid(row=4, column=1, sticky="ew", pady=2)
            ttk_module.Label(merge_tab, text="Output PDF").grid(row=5, column=0, sticky="w", pady=(12, 3))
            ttk_module.Entry(merge_tab, textvariable=self.merge_output).grid(row=6, column=0, sticky="ew", padx=(0, 8))
            ttk_module.Button(merge_tab, text="Browse…", command=self._browse_merge_output).grid(row=6, column=1, sticky="ew")
            self.merge_button = ttk_module.Button(merge_tab, text="Merge in listed order", command=self._start_merge)
            self.merge_button.grid(row=7, column=1, sticky="e", pady=(10, 0))

            split_tab = ttk_module.Frame(notebook, padding=12)
            notebook.add(split_tab, text="Split PDF")
            split_tab.columnconfigure(1, weight=1)
            ttk_module.Label(split_tab, text="Input PDF").grid(row=0, column=0, sticky="w", pady=6)
            ttk_module.Entry(split_tab, textvariable=self.split_source).grid(row=0, column=1, sticky="ew", padx=8)
            ttk_module.Button(split_tab, text="Browse…", command=self._browse_split_source).grid(row=0, column=2)
            ttk_module.Label(split_tab, text="Page groups").grid(row=1, column=0, sticky="w", pady=6)
            ttk_module.Entry(split_tab, textvariable=self.split_groups).grid(row=1, column=1, sticky="ew", padx=8)
            ttk_module.Label(split_tab, text="Example: 1-3;4-6;7. All pages must be included once.", wraplength=220).grid(row=1, column=2, sticky="w")
            ttk_module.Label(split_tab, text="New output folder").grid(row=2, column=0, sticky="w", pady=6)
            ttk_module.Entry(split_tab, textvariable=self.split_output).grid(row=2, column=1, sticky="ew", padx=8)
            ttk_module.Button(split_tab, text="Choose parent…", command=self._choose_split_parent).grid(row=2, column=2)
            self.split_button = ttk_module.Button(split_tab, text="Split into parts", command=self._start_split)
            self.split_button.grid(row=3, column=2, sticky="e", pady=(10, 0))

            ttk_module.Separator(outer).pack(fill="x", pady=(10, 8))
            ttk_module.Label(outer, textvariable=self.status, wraplength=720).pack(fill="x", anchor="w")
            self.progress = ttk_module.Progressbar(outer, mode="determinate", maximum=1, variable=self.progress_value)
            self.progress.pack(fill="x", pady=(7, 0))
            ttk_module.Label(outer, text="Offline. Rewriting PDFs may not preserve signatures, forms, bookmarks, links or all metadata. Review outputs before use.", wraplength=720).pack(fill="x", anchor="w", pady=(7, 0))

        def _refresh_list(self) -> None:
            self.listbox.delete(0, tk.END)
            for path in self.inputs:
                self.listbox.insert(tk.END, path)

        def _add_files(self) -> None:
            chosen = filedialog.askopenfilenames(title="Choose PDFs in merge order", filetypes=[("PDF files", "*.pdf")])
            for item in chosen:
                candidate = _lexical_absolute(item)
                if not any(_same_file(candidate, Path(existing)) for existing in self.inputs):
                    self.inputs.append(item)
            self._refresh_list()
            if self.inputs and not self.merge_output.get():
                first = Path(self.inputs[0])
                self.merge_output.set(str(first.with_name("merged.pdf")))

        def _remove_files(self) -> None:
            for index in reversed(self.listbox.curselection()):
                del self.inputs[index]
            self._refresh_list()

        def _move(self, delta: int) -> None:
            selected = list(self.listbox.curselection())
            if not selected:
                return
            indices = range(len(selected)) if delta < 0 else range(len(selected) - 1, -1, -1)
            for position in indices:
                index = selected[position]
                destination = index + delta
                if 0 <= destination < len(self.inputs) and destination not in selected:
                    self.inputs[index], self.inputs[destination] = self.inputs[destination], self.inputs[index]
                    selected[position] = destination
            self._refresh_list()
            for index in selected:
                self.listbox.selection_set(index)

        def _browse_merge_output(self) -> None:
            path = filedialog.asksaveasfilename(title="Save merged PDF", defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")])
            if path:
                self.merge_output.set(path)

        def _browse_split_source(self) -> None:
            path = filedialog.askopenfilename(title="Choose a PDF to split", filetypes=[("PDF files", "*.pdf")])
            if path:
                self.split_source.set(path)
                source = Path(path)
                self.split_output.set(str(source.with_name(source.stem + "_parts")))
                try:
                    pages = inspect_pdf(path).pages
                    self.status.set(f"{pages} pages. Enter semicolon-separated groups that cover pages 1–{pages} exactly once.")
                    if pages > 1:
                        boundary = pages // 2
                        self.split_groups.set(f"1-{boundary};{boundary + 1}-{pages}")
                    else:
                        self.split_groups.set("")
                except ValueError as exc:
                    self.status.set(str(exc))

        def _choose_split_parent(self) -> None:
            parent = filedialog.askdirectory(title="Choose the parent folder for a new split-output folder")
            if parent:
                source_name = Path(self.split_source.get()).stem if self.split_source.get() else "split"
                self.split_output.set(str(Path(parent) / f"{source_name}_parts"))

        def _submit(self, operation: str, work: Callable[[], object]) -> None:
            if self.busy:
                return
            self.busy = True
            self.merge_button.state(["disabled"])
            self.split_button.state(["disabled"])
            self.status.set(operation)
            self.progress_value.set(0)
            self.progress.configure(maximum=1)

            def worker() -> None:
                try:
                    events.put(("done", work()))
                except Exception as exc:
                    events.put(("error", str(exc)))
            threading.Thread(target=worker, daemon=True).start()

        def _start_merge(self) -> None:
            sources = tuple(self.inputs)
            output = self.merge_output.get().strip()
            if len(sources) < 2 or not output:
                messagebox.showerror(APP_NAME, "Add at least two PDFs and choose an output path.")
                return
            overwrite = False
            if Path(output).exists():
                if not messagebox.askyesno(APP_NAME, "The output file exists. Replace it?"):
                    return
                overwrite = True
            self._submit("Merging PDFs…", lambda: merge_pdfs(sources, output, overwrite=overwrite,
                                                              progress=lambda current, total: events.put(("progress", (current, total)))))

        def _start_split(self) -> None:
            source = self.split_source.get().strip()
            output = self.split_output.get().strip()
            groups = self.split_groups.get().strip()
            if not source or not output:
                messagebox.showerror(APP_NAME, "Choose an input PDF and a new output folder.")
                return
            if Path(output).exists():
                messagebox.showerror(APP_NAME, "The output folder already exists. Choose a new folder name.")
                return
            self._submit("Splitting PDF…", lambda: split_pdf(source, output, groups,
                                                              progress=lambda current, total: events.put(("progress", (current, total)))))

        @staticmethod
        def _result_text(value: object) -> str:
            if isinstance(value, MergeResult):
                return f"Merged {value.input_files} PDFs into:\n{value.output_path}\nPages: {value.pages}"
            if isinstance(value, SplitResult):
                return f"Created {len(value.files)} PDF parts in:\n{value.output_dir}"
            return str(value)

        def _poll(self) -> None:
            while True:
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    current, total = value
                    self.progress.configure(maximum=total)
                    self.progress_value.set(current)
                    self.status.set(f"Processing {current} of {total}…")
                elif kind == "done":
                    self.busy = False
                    self.merge_button.state(["!disabled"])
                    self.split_button.state(["!disabled"])
                    self.status.set("Completed. Review the output before using it.")
                    messagebox.showinfo(APP_NAME, self._result_text(value))
                elif kind == "error":
                    self.busy = False
                    self.merge_button.state(["!disabled"])
                    self.split_button.state(["!disabled"])
                    self.status.set("Operation failed; no incomplete output should be used.")
                    messagebox.showerror(APP_NAME, str(value))
            self.root.after(100, self._poll)

    root = tk.Tk()
    App(root)
    root.mainloop()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        launch_gui()
        return 0
    return run_cli(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
