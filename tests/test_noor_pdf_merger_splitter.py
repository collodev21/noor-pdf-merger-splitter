from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter, legal
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import noor_pdf_merger_splitter as pdf_tool


class PDFMergerSplitterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_pdf(self, path: Path, labels: list[str], sizes=None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        page_sizes = sizes or [letter] * len(labels)
        document = canvas.Canvas(str(path), pagesize=page_sizes[0])
        for label, size in zip(labels, page_sizes):
            document.setPageSize(size)
            document.drawString(72, size[1] - 72, label)
            document.showPage()
        document.save()
        return path

    @staticmethod
    def extract_labels(path: Path) -> list[str]:
        return [(page.extract_text() or "").strip() for page in PdfReader(str(path)).pages]

    def test_merge_preserves_input_order_text_mixed_sizes_and_source_bytes(self):
        first = self.make_pdf(self.root / "first.pdf", ["FIRST A", "FIRST B"], [letter, legal])
        second = self.make_pdf(self.root / "second.pdf", ["SECOND"], [letter])
        before = (first.read_bytes(), second.read_bytes())
        output = self.root / "merged.pdf"
        progress = []

        result = pdf_tool.merge_pdfs([first, second], output, progress=lambda current, total: progress.append((current, total)))

        self.assertEqual(result.input_files, 2)
        self.assertEqual(result.pages, 3)
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual(self.extract_labels(output), ["FIRST A", "FIRST B", "SECOND"])
        reader = PdfReader(str(output))
        self.assertAlmostEqual(float(reader.pages[0].mediabox.width), letter[0])
        self.assertAlmostEqual(float(reader.pages[1].mediabox.width), legal[0])
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)

    def test_merge_requires_two_distinct_inputs(self):
        first = self.make_pdf(self.root / "first.pdf", ["FIRST"])
        with self.assertRaisesRegex(ValueError, "at least 2"):
            pdf_tool.merge_pdfs([first], self.root / "out.pdf")
        with self.assertRaisesRegex(ValueError, "more than once"):
            pdf_tool.merge_pdfs([first, first], self.root / "out.pdf")

    def test_merge_protects_input_path_and_existing_output(self):
        first = self.make_pdf(self.root / "first.pdf", ["FIRST"])
        second = self.make_pdf(self.root / "second.pdf", ["SECOND"])
        original_first = first.read_bytes()
        with self.assertRaisesRegex(ValueError, "input PDFs"):
            pdf_tool.merge_pdfs([first, second], first)
        output = self.root / "existing.pdf"
        output.write_bytes(b"keep this")
        with self.assertRaises(FileExistsError):
            pdf_tool.merge_pdfs([first, second], output)
        self.assertEqual(output.read_bytes(), b"keep this")
        pdf_tool.merge_pdfs([first, second], output, overwrite=True)
        self.assertEqual(self.extract_labels(output), ["FIRST", "SECOND"])
        self.assertEqual(first.read_bytes(), original_first)

    def test_merge_rejects_corrupt_and_encrypted_inputs(self):
        corrupt = self.root / "broken.pdf"
        corrupt.write_bytes(b"not a PDF file")
        valid = self.make_pdf(self.root / "valid.pdf", ["VALID"])
        with self.assertRaises(ValueError):
            pdf_tool.merge_pdfs([valid, corrupt], self.root / "bad.pdf")

        writer = PdfWriter()
        writer.append(str(valid))
        writer.encrypt("secret")
        encrypted = self.root / "locked.pdf"
        writer.write(str(encrypted))
        writer.close()
        with self.assertRaisesRegex(ValueError, "Password-protected"):
            pdf_tool.inspect_pdf(encrypted)

    def test_merge_enforces_total_size_and_page_limits(self):
        first = self.make_pdf(self.root / "first.pdf", ["FIRST"])
        second = self.make_pdf(self.root / "second.pdf", ["SECOND"])
        with patch.object(pdf_tool, "MAX_TOTAL_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "combined"):
                pdf_tool.merge_pdfs([first, second], self.root / "too-large.pdf")
        with patch.object(pdf_tool, "MAX_PAGES_PER_FILE", 1):
            multi = self.make_pdf(self.root / "multi.pdf", ["A", "B"])
            with self.assertRaisesRegex(ValueError, "page per-file limit"):
                pdf_tool.inspect_pdf(multi)

    def test_parse_groups_accepts_a_complete_partition(self):
        self.assertEqual(
            pdf_tool.parse_split_groups("1-3; 4 ;5-8", 8),
            ((1, 3), (4, 4), (5, 8)),
        )
        self.assertEqual(pdf_tool.parse_split_groups("1;2", 2), ((1, 1), (2, 2)))

    def test_parse_groups_rejects_gaps_overlaps_bad_ranges_and_single_group(self):
        invalid = [
            "", "1-8", "1-3;5-8", "1-5;5-8", "2-4;5-8", "1-3;4-9",
            "1-3;;4-8", "3-1;2-8", "abc;4-8", "1-3;4-7", "1;1;2-8",
        ]
        for specification in invalid:
            with self.subTest(specification=specification):
                with self.assertRaises(ValueError):
                    pdf_tool.parse_split_groups(specification, 8)
        with self.assertRaisesRegex(ValueError, "at least two"):
            pdf_tool.parse_split_groups("1", 1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            pdf_tool.parse_split_groups("1-2", 0)

    def test_split_writes_page_groups_with_text_and_keeps_source(self):
        source = self.make_pdf(self.root / "source.pdf", ["PAGE ONE", "PAGE TWO", "PAGE THREE", "PAGE FOUR"])
        before = source.read_bytes()
        output_dir = self.root / "split-parts"
        progress = []
        result = pdf_tool.split_pdf(source, output_dir, "1-2;3;4", progress=lambda current, total: progress.append((current, total)))
        self.assertEqual(result.page_groups, ((1, 2), (3, 3), (4, 4)))
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])
        self.assertEqual([self.extract_labels(path) for path in result.files],
                         [["PAGE ONE", "PAGE TWO"], ["PAGE THREE"], ["PAGE FOUR"]])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(len(list(output_dir.glob("*.pdf"))), 3)

    def test_split_refuses_existing_output_directory(self):
        source = self.make_pdf(self.root / "source.pdf", ["ONE", "TWO"])
        output_dir = self.root / "existing"
        output_dir.mkdir()
        sentinel = output_dir / "keep.txt"
        sentinel.write_text("safe", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            pdf_tool.split_pdf(source, output_dir, "1;2")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")

    def test_split_invalid_plan_creates_no_output_or_staging_directory(self):
        source = self.make_pdf(self.root / "source.pdf", ["ONE", "TWO", "THREE"])
        output_dir = self.root / "parts"
        with self.assertRaises(ValueError):
            pdf_tool.split_pdf(source, output_dir, "1;3")
        self.assertFalse(output_dir.exists())
        self.assertEqual(list(self.root.glob(".parts.tmp-*")), [])

    def test_split_write_failure_cleans_staging_folder(self):
        source = self.make_pdf(self.root / "source.pdf", ["ONE", "TWO"])
        output_dir = self.root / "parts"
        with patch.object(PdfWriter, "write", side_effect=OSError("simulated disk error")):
            with self.assertRaisesRegex(OSError, "simulated disk error"):
                pdf_tool.split_pdf(source, output_dir, "1;2")
        self.assertFalse(output_dir.exists())
        self.assertEqual(list(self.root.glob(".parts.tmp-*")), [])

    def test_merge_write_failure_leaves_no_partial_output_or_temporary_file(self):
        first = self.make_pdf(self.root / "first.pdf", ["FIRST"])
        second = self.make_pdf(self.root / "second.pdf", ["SECOND"])
        output = self.root / "merged.pdf"
        with patch.object(PdfWriter, "write", side_effect=OSError("simulated disk error")):
            with self.assertRaisesRegex(OSError, "simulated disk error"):
                pdf_tool.merge_pdfs([first, second], output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".merged.tmp-*.pdf")), [])

    def test_unicode_paths_work_for_merge_and_split(self):
        folder = self.root / "مكتبة-é"
        first = self.make_pdf(folder / "وثيقة-١.pdf", ["UNICODE ONE"])
        second = self.make_pdf(folder / "وثيقة-٢.pdf", ["UNICODE TWO"])
        merged = folder / "ملف-مجمع.pdf"
        pdf_tool.merge_pdfs([first, second], merged)
        self.assertEqual(self.extract_labels(merged), ["UNICODE ONE", "UNICODE TWO"])
        split = folder / "ملف-مقسم"
        result = pdf_tool.split_pdf(merged, split, "1;2")
        self.assertEqual([self.extract_labels(path) for path in result.files], [["UNICODE ONE"], ["UNICODE TWO"]])

    def test_cli_help(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exit_info:
                pdf_tool.run_cli(["--help"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("merge", stdout.getvalue())
        self.assertIn("split", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
