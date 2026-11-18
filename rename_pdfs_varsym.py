#!/usr/bin/env python3
"""
POHODA PDF Renamer - Variable Symbol Matching (IN-PLACE RENAME)

Renames PDF invoices by extracting the Variable Symbol from the PDF,
looking it up in the MDB database (FA table, varsym column), and
renaming to: cislo_originalname.pdf

Workflow:
1. Extract text from ALL pages of PDF (full document scan)
2. Find "Variabilní symbol" / "Variable Symbol" / "VS" anywhere in PDF text
3. Open MDB, find table starting with 'FA' (faktury/invoices)
4. In that table, find 'varsym' column and 'cislo' column
5. Match PDF's variable symbol to MDB's varsym
6. Get corresponding 'cislo' from same row
7. Rename PDF to: {cislo}_{original_filename}.pdf

Requirements:
    mdbtools (for mdb-export, mdb-tables)
      Arch: sudo pacman -S mdbtools
      Debian/Ubuntu: sudo apt install mdbtools
      Fedora: sudo dnf install mdbtools
      macOS: brew install mdbtools
    Python: pip install pandas pypdf
"""

import argparse
import re
import sys
import shutil
import subprocess
import io
import queue
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    print("Error: pandas not installed. Run: pip install pandas pypdf", file=sys.stderr)
    sys.exit(1)

try:
    from pypdf import PdfReader
except ImportError:
    print("Error: pypdf not installed. Run: pip install pypdf", file=sys.stderr)
    sys.exit(1)


# ============================================================
# Column aliases for POHODA MDB export
# ============================================================
COLUMN_ALIASES = {
    'varsym': [
        'varsym', 'var. symb.', 'variabilni symbol', 'variabilní symbol',
        'vs', 'variable_symbol', 'var_sym', 'vsym', 'symbol', 'var symbol'
    ],
    'cislo': [
        'cislo', 'číslo', 'cislo_dokladu', 'číslo dokladu', 'document_number',
        'doc_number', 'doklad', 'invoice_number', 'faktura_cislo', 'č.faktury',
        'cislo_faktury', 'číslo faktury', 'cislo_doklad', 'číslo doklad',
        'cislo_dokladu', 'číslo_dokladu'
    ],
    'firma': [
        'firma', 'company', 'dodavatel', 'dodavatel_nazev', 'dodavatel_název',
        'supplier', 'vendor', 'partner', 'partner_name', 'name', 'nazev', 'název',
        'odběratel', 'odberatel', 'customer', 'client', 'dodavatel_jmeno',
        'dodavatel_jméno', 'dodavatel_nazev', 'partner_nazev', 'partner_název',
        'nazev_firmy', 'název_firmy', 'firma_nazev', 'firma_název'
    ],
    'doklad': [
        'doklad', 'document', 'doctype', 'doc_type', 'typ_dokladu', 'typ_doklad',
        'doklad_typ', 'druh_dokladu', 'druh_doklad', 'invoice_type', 'typ',
        'faktura_typ', 'zdroj', 'source',
        'doklad_zkratka', 'doklad_zkrat', 'zkratka', 'kod', 'code',
        'typ_dokladu_zkratka', 'druh', 'druh_dokladu_kod',
        'druhdokladu', 'typdokladu', 'zdrojdokladu', 'dokladzkratka',
        'druhdokladukod', 'koddokladu'
    ],
}


def normalize_column_name(name: str) -> str:
    return re.sub(r'[\s_\-./]+', '', name.lower().strip())


def find_column(df_columns: List[str], target: str) -> Optional[str]:
    """Find the actual column name in DataFrame matching the target."""
    aliases = COLUMN_ALIASES.get(target, [])
    normalized_cols = {normalize_column_name(c): c for c in df_columns}

    # Try exact alias match first
    for alias in aliases:
        norm_alias = normalize_column_name(alias)
        if norm_alias in normalized_cols:
            return normalized_cols[norm_alias]

    # Try exact match on full column name only
    for alias in aliases:
        norm_alias = normalize_column_name(alias)
        for norm_col, orig_col in normalized_cols.items():
            if norm_alias == norm_col:
                return orig_col

    return None


def find_newest_mdb(folder: Path, pattern: str) -> Optional[Path]:
    """
    Find the newest .mdb file in a folder matching the given pattern.
    Pattern can be a partial filename (case-insensitive substring match).
    Returns the Path to the newest matching file, or None if not found.
    """
    if not folder.exists() or not folder.is_dir():
        return None

    pattern_lower = pattern.lower()
    mdb_files = list(folder.rglob("*.mdb"))

    matching = [f for f in mdb_files if pattern_lower in f.name.lower()]

    if not matching:
        return None

    # Sort by modification time, newest first
    matching.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return matching[0]


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = re.sub(r'[_\s]+', '_', name)
    name = name.strip('_.')
    if len(name) > 200:
        name = name[:200].rstrip('_.')
    return name or "unknown"


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from all pages of a PDF.
    Scans the entire document to find variable symbol anywhere.
    """
    try:
        reader = PdfReader(str(pdf_path))
        texts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                texts.append(text)
        return " ".join(texts)
    except Exception as e:
        return f"[ERROR reading PDF: {e}]"


def extract_variable_symbol(text: str) -> Optional[str]:
    """
    Extract variable symbol (variabilní symbol / variabilny symbol) from PDF text.
    Comprehensive multi-pass approach:
    1. Collect ALL candidate matches with context scoring
    2. Rank by confidence (explicit patterns > fuzzy > fallback)
    3. Return best candidate

    Handles Czech, Slovak, English variants with/without diacritics, OCR errors.
    """
    text_upper = text.upper()

    # Collect all candidates with their confidence scores
    candidates = []  # list of (score, candidate, pattern_type)

    # Define pattern groups with base scores (higher = more confident)
    pattern_groups = [
        # Group 1: Explicit "variable symbol" patterns (highest confidence: 100)
        (100, [
            r'(?:VARIABILN[IYÍ]\s*SYMBOL|VARIABILN[YÝ]\s*SYMBOL)\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABLE\s*SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILNY\s+SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILNI\s+SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILN[IYÍÝ]\s*(?:S[IÍ]MBOLO|S[IÍ]MBOL)\b\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILN[YÝ]\s*(?:S[IÍ]MBOLO|S[IÍ]MBOL)\b\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'\bV\.?S\.?\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VAR\.?\s*(?:SYMB|SYMBOL)\.?\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'(?:VARIABILN[IYÍÝ]\s*SYMBOL|VARIABILN[YÝ]\s*SYMBOL)\b\s+([A-Z0-9\-\/]{6,})',
            r'VARIABILN[YÝ]\s*SYMBOL\s+([A-Z0-9\-\/]{6,})',
            r'(?:VARIABILN[IYÍÝ]\s*|VARIABLE\s+)SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
        ]),

        # Group 2: Strong keyword + code proximity (confidence: 80)
        (80, [
            r'VARIABILN[IYÍÝ]\b.{0,20}?([A-Z0-9\-\/]{6,})',
            r'VARIABILN[YÝ]\b.{0,20}?([A-Z0-9\-\/]{6,})',
            r'VARIABLE\b.{0,20}?([A-Z0-9\-\/]{6,})',
            r'\bVAR\b.{0,15}?([A-Z0-9\-\/]{6,})',
            r'\bV\.?S\.?\b.{0,15}?([A-Z0-9\-\/]{6,})',
            r'(?:SYMBOL|SIMBOL|SIMBOLO|S[IÍ]MBOL)\b.{0,10}?([A-Z0-9\-\/]{6,})',
        ]),

        # Group 3: Keyword within 30 chars of alphanumeric code (confidence: 60)
        (60, [
            r'(?:VARIABILN[IYÍÝ]|VARIABILN[YÝ]|VARIABLE|VAR\b|VS\b|V\.S\.|SYMBOL|SIMBOL|SIMBOLO|S[IÍ]MBOL).{0,30}?([A-Z0-9]{6,})',
        ]),

        # Group 4: Common reference formats near "faktura"/"invoice"/"cislo" (confidence: 50)
        (50, [
            r'(?:FAKTURA|FAKT[ÚU]RA|INVOICE|ČÍSLO|CISLO|DOKLAD|DOKLADU|REFERENCE|REF)\b.{0,30}?([A-Z]{2,4}[0-9]{4,})',
            r'(?:FAKTURA|FAKT[ÚU]RA|INVOICE|ČÍSLO|CISLO|DOKLAD|DOKLADU|REFERENCE|REF)\b.{0,30}?([0-9]{6,})',
        ]),

        # Group 5: Fallback - any reference-like code (confidence: 30)
        (30, [
            r'\b([A-Z]{2,4}[0-9]{4,})\b',
            r'\b([0-9]{7,})\b',  # 7+ digits to avoid years
        ]),
    ]

    # False positive filters
    FALSE_POSITIVES = {
        '2022', '2023', '2024', '2025', '2026', '2027', '2028', '2029', '2030',
        'EUR', 'CZK', 'USD', 'DPH', 'KCE', 'SK', 'CZ', 'PLN', 'HUF',
        'TOTAL', 'CELKEM', 'SUMA', 'CASTKA', 'CASTKA', 'AMOUNT',
        'DATUM', 'DATE', 'SPLATNOST', 'DODANI', 'VYSTAVENI',
        'ICO', 'DIC', 'ID', 'UID', 'PID',
    }

    # Process each pattern group
    for base_score, patterns in pattern_groups:
        for pattern in patterns:
            for match in re.finditer(pattern, text_upper, re.IGNORECASE):
                candidate = match.group(1).strip().upper()
                # Clean up candidate
                candidate = re.sub(r'[^\w\-\/]+$', '', candidate)

                # Validation
                if not candidate or len(candidate) < 4:
                    continue
                if not re.search(r'\d', candidate):  # Must contain digit
                    continue
                if candidate in FALSE_POSITIVES:
                    continue
                if re.match(r'^20\d{2}$', candidate):  # Years
                    continue
                # Avoid pure words
                if re.match(r'^[A-Z]{4,}$', candidate) and not re.search(r'\d', candidate):
                    continue

                # Bonus: longer codes are more likely to be real variable symbols
                length_bonus = min(len(candidate) - 4, 10)  # up to +10 for length

                # Bonus: contains separator (/, -) typical for Czech variable symbols
                separator_bonus = 5 if re.search(r'[\-/]', candidate) else 0

                # Bonus: appears in context of "vs" or "variabilni"
                context_bonus = 0
                match_start = match.start(1)
                context_window = text_upper[max(0, match_start-50):match_start+50]
                if 'VARIABILN' in context_window or 'VARIABLE' in context_window:
                    context_bonus = 10
                elif 'VS' in context_window:
                    context_bonus = 5

                total_score = base_score + length_bonus + separator_bonus + context_bonus
                candidates.append((total_score, candidate, pattern))

    if not candidates:
        return None

    # Sort by score descending, then by length descending (prefer longer)
    candidates.sort(key=lambda x: (x[0], len(x[1])), reverse=True)

    best_candidate = candidates[0][1]

    # Debug: print top candidates if verbose
    # print(f"  Top candidates: {[(c[1], c[0]) for c in candidates[:5]]}")

    return best_candidate


def extract_all_variable_symbol_candidates(text: str) -> List[Tuple[int, str, str]]:
    """
    Extract ALL variable symbol candidates with scores for debugging.
    Returns list of (score, candidate, pattern_type).
    """
    text_upper = text.upper()
    candidates = []

    pattern_groups = [
        (100, "explicit", [
            r'(?:VARIABILN[IYÍ]\s*SYMBOL|VARIABILN[YÝ]\s*SYMBOL)\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABLE\s*SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILNY\s+SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILNI\s+SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILN[IYÍÝ]\s*(?:S[IÍ]MBOLO|S[IÍ]MBOL)\b\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VARIABILN[YÝ]\s*(?:S[IÍ]MBOLO|S[IÍ]MBOL)\b\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'\bV\.?S\.?\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'VAR\.?\s*(?:SYMB|SYMBOL)\.?\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
            r'(?:VARIABILN[IYÍÝ]\s*SYMBOL|VARIABILN[YÝ]\s*SYMBOL)\b\s+([A-Z0-9\-\/]{6,})',
            r'VARIABILN[YÝ]\s*SYMBOL\s+([A-Z0-9\-\/]{6,})',
            r'(?:VARIABILN[IYÍÝ]\s*|VARIABLE\s+)SYMBOL\s*[:\-]?\s*([A-Z0-9\-\/]{4,})',
        ]),
        (80, "keyword_proximity", [
            r'VARIABILN[IYÍÝ]\b.{0,20}?([A-Z0-9\-\/]{6,})',
            r'VARIABILN[YÝ]\b.{0,20}?([A-Z0-9\-\/]{6,})',
            r'VARIABLE\b.{0,20}?([A-Z0-9\-\/]{6,})',
            r'\bVAR\b.{0,15}?([A-Z0-9\-\/]{6,})',
            r'\bV\.?S\.?\b.{0,15}?([A-Z0-9\-\/]{6,})',
            r'(?:SYMBOL|SIMBOL|SIMBOLO|S[IÍ]MBOL)\b.{0,10}?([A-Z0-9\-\/]{6,})',
        ]),
        (60, "keyword_nearby", [
            r'(?:VARIABILN[IYÍÝ]|VARIABILN[YÝ]|VARIABLE|VAR\b|VS\b|V\.S\.|SYMBOL|SIMBOL|SIMBOLO|S[IÍ]MBOL).{0,30}?([A-Z0-9]{6,})',
        ]),
        (50, "invoice_context", [
            r'(?:FAKTURA|FAKT[ÚU]RA|INVOICE|ČÍSLO|CISLO|DOKLAD|DOKLADU|REFERENCE|REF)\b.{0,30}?([A-Z]{2,4}[0-9]{4,})',
            r'(?:FAKTURA|FAKT[ÚU]RA|INVOICE|ČÍSLO|CISLO|DOKLAD|DOKLADU|REFERENCE|REF)\b.{0,30}?([0-9]{6,})',
        ]),
        (30, "fallback", [
            r'\b([A-Z]{2,4}[0-9]{4,})\b',
            r'\b([0-9]{7,})\b',
        ]),
    ]

    FALSE_POSITIVES = {
        '2022', '2023', '2024', '2025', '2026', '2027', '2028', '2029', '2030',
        'EUR', 'CZK', 'USD', 'DPH', 'KCE', 'SK', 'CZ', 'PLN', 'HUF',
        'TOTAL', 'CELKEM', 'SUMA', 'CASTKA', 'CASTKA', 'AMOUNT',
        'DATUM', 'DATE', 'SPLATNOST', 'DODANI', 'VYSTAVENI',
        'ICO', 'DIC', 'ID', 'UID', 'PID',
    }

    for base_score, pattern_type, patterns in pattern_groups:
        for pattern in patterns:
            for match in re.finditer(pattern, text_upper, re.IGNORECASE):
                candidate = match.group(1).strip().upper()
                candidate = re.sub(r'[^\w\-\/]+$', '', candidate)

                if not candidate or len(candidate) < 4:
                    continue
                if not re.search(r'\d', candidate):
                    continue
                if candidate in FALSE_POSITIVES:
                    continue
                if re.match(r'^20\d{2}$', candidate):
                    continue
                if re.match(r'^[A-Z]{4,}$', candidate) and not re.search(r'\d', candidate):
                    continue

                length_bonus = min(len(candidate) - 4, 10)
                separator_bonus = 5 if re.search(r'[\-/]', candidate) else 0

                match_start = match.start(1)
                context_window = text_upper[max(0, match_start-50):match_start+50]
                context_bonus = 0
                if 'VARIABILN' in context_window or 'VARIABLE' in context_window:
                    context_bonus = 10
                elif 'VS' in context_window:
                    context_bonus = 5

                total_score = base_score + length_bonus + separator_bonus + context_bonus
                candidates.append((total_score, candidate, pattern_type))

    candidates.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
    return candidates


def find_fa_table(mdb_path: Path) -> Optional[str]:
    """
    Find the FA (Faktury/Invoices) table in the MDB.
    POHODA typically names invoice tables starting with 'FA'.
    """
    try:
        result = subprocess.run(
            ['mdb-tables', '-1', str(mdb_path)],
            capture_output=True,
            text=True,
            check=True
        )
        tables = [t.strip() for t in result.stdout.strip().split('\n') if t.strip()]
    except subprocess.CalledProcessError as e:
        raise ValueError(f"Failed to list tables in MDB file: {e.stderr}")

    # Look for tables starting with FA (case insensitive)
    for table in tables:
        if table.upper().startswith('FA'):
            return table

    # Fallback: first table
    if tables:
        return tables[0]

    return None


def load_varsym_to_cislo_map(mdb_path: Path) -> Tuple[Dict[str, str], str]:
    """
    Load the mapping from varsym -> cislo from the FA table.
    Returns (varsym_to_cislo_dict, table_name_used)
    """
    if not mdb_path.exists():
        raise FileNotFoundError(f"MDB file not found: {mdb_path}")

    # Check if mdb-tools is available
    for cmd in ['mdb-tables', 'mdb-export']:
        if not shutil.which(cmd):
            raise ValueError(
                f"Required command '{cmd}' not found. Please install mdbtools:\n"
                f"  Arch: sudo pacman -S mdbtools\n"
                f"  Debian/Ubuntu: sudo apt install mdbtools\n"
                f"  Fedora: sudo dnf install mdbtools\n"
                f"  macOS: brew install mdbtools"
            )

    # Find FA table
    table_name = find_fa_table(mdb_path)
    if not table_name:
        raise ValueError("No tables found in MDB file.")

    print(f"Using table: {table_name}")

    # Export table to CSV using mdb-export
    try:
        result = subprocess.run(
            ['mdb-export', str(mdb_path), table_name],
            capture_output=True,
            text=True,
            check=True
        )
        csv_data = result.stdout
    except subprocess.CalledProcessError as e:
        raise ValueError(f"Failed to export table '{table_name}': {e.stderr}")

    # Parse CSV with pandas
    try:
        df = pd.read_csv(io.StringIO(csv_data))
    except Exception as e:
        raise ValueError(f"Failed to parse CSV export: {e}")

    if df.empty:
        raise ValueError("MDB table is empty.")

    cols = df.columns.tolist()
    print(f"Available columns: {cols}")

    # Find varsym, cislo, firma, and doklad columns
    varsym_col = find_column(cols, 'varsym')
    cislo_col = find_column(cols, 'cislo')
    firma_col = find_column(cols, 'firma')
    doklad_col = find_column(cols, 'doklad')

    print(f"Detected columns:")
    print(f"  varsym: '{varsym_col}'" if varsym_col else "  varsym: NOT FOUND")
    print(f"  cislo:  '{cislo_col}'" if cislo_col else "  cislo:  NOT FOUND")
    print(f"  firma:  '{firma_col}'" if firma_col else "  firma:  NOT FOUND")
    print(f"  doklad: '{doklad_col}'" if doklad_col else "  doklad: NOT FOUND")

    if not varsym_col:
        available = ", ".join(str(c) for c in cols)
        raise ValueError(f"Missing required 'varsym' column.\nAvailable: {available}")
    if not cislo_col:
        available = ", ".join(str(c) for c in cols)
        raise ValueError(f"Missing required 'cislo' column.\nAvailable: {available}")

    # Build mapping: varsym -> (cislo, firma, doklad)
    varsym_to_data = {}
    sample_printed = 0
    for _, row in df.iterrows():
        varsym_val = row[varsym_col]
        cislo_val = row[cislo_col]
        firma_val = row[firma_col] if firma_col and pd.notna(row[firma_col]) else ""
        doklad_val = row[doklad_col] if doklad_col and pd.notna(row[doklad_col]) else ""

        if pd.notna(varsym_val) and pd.notna(cislo_val):
            varsym_key = str(varsym_val).strip().upper()
            cislo_value = str(cislo_val).strip()
            firma_value = str(firma_val).strip() if firma_val else ""
            doklad_value = str(doklad_val).strip() if doklad_val else ""
            if varsym_key and cislo_value:
                varsym_to_data[varsym_key] = (cislo_value, firma_value, doklad_value)
                # Debug: print first few samples
                if sample_printed < 3:
                    print(f"  Sample: varsym='{varsym_key}' -> cislo='{cislo_value}', firma='{firma_value}', doklad='{doklad_value}'")
                    sample_printed += 1

    print(f"Loaded {len(varsym_to_data)} varsym->(cislo,firma,doklad) mappings from MDB.")
    return varsym_to_data, table_name


def process_pdfs(
    source_dir: Path,
    output_dir: Path,
    varsym_to_data: Dict[str, Tuple[str, str, str]],
    copy_mode: bool = True,
    unmatched_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Tuple[List[dict], List[dict]]:
    pdf_files = list(source_dir.rglob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in {source_dir}")
        return [], []

    print(f"Found {len(pdf_files)} PDF file(s) to process.")

    matched_results = []
    unmatched_results = []

    for pdf_path in pdf_files:
        print(f"\nProcessing: {pdf_path.name}")

        text = extract_text_from_pdf(pdf_path)
        if text.startswith("[ERROR"):
            result = {'pdf_path': pdf_path, 'matched': False, 'reason': text}
            unmatched_results.append(result)
            print(f"  [ERROR] {text}")
            continue

        # Extract variable symbol from PDF
        pdf_varsym = extract_variable_symbol(text)

        if not pdf_varsym:
            result = {'pdf_path': pdf_path, 'matched': False, 'reason': "No variable symbol found in PDF"}
            unmatched_results.append(result)
            print(f"  [WARN] No variable symbol found in PDF")
            continue

        print(f"  Found variable symbol in PDF: {pdf_varsym}")

        # Look up in MDB mapping
        data = varsym_to_data.get(pdf_varsym)

        if not data:
            result = {'pdf_path': pdf_path, 'matched': False, 'reason': f"Variable symbol '{pdf_varsym}' not found in MDB", 'pdf_varsym': pdf_varsym}
            unmatched_results.append(result)
            print(f"  [WARN] Variable symbol '{pdf_varsym}' not found in MDB")
            continue

        cislo, firma, doklad = data

        # Create new filename: cislo_firma_doklad.pdf (skip empty parts)
        parts = [sanitize_filename(cislo)]
        if firma:
            parts.append(sanitize_filename(firma))
        if doklad:
            parts.append(sanitize_filename(doklad))
        new_name = "_".join(parts) + ".pdf"

        result = {
            'pdf_path': pdf_path,
            'matched': True,
            'cislo': cislo,
            'firma': firma,
            'doklad': doklad,
            'pdf_varsym': pdf_varsym,
            'new_filename': new_name,
            'reason': f"Matched varsym '{pdf_varsym}' -> cislo '{cislo}', firma '{firma}', doklad '{doklad}'",
        }
        matched_results.append(result)

        print(f"  [OK] {result['reason']}")
        print(f"     New name: {new_name}")

        if not dry_run:
            dest_path = output_dir / new_name
            counter = 1
            original_dest = dest_path
            while dest_path.exists():
                stem = original_dest.stem
                suffix = original_dest.suffix
                dest_path = output_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            try:
                if copy_mode:
                    shutil.copy2(pdf_path, dest_path)
                    print(f"     -> Copied to {dest_path.name}")
                else:
                    shutil.move(str(pdf_path), str(dest_path))
                    print(f"     -> Renamed to {dest_path.name}")
            except Exception as e:
                print(f"     [ERROR] Error: {e}")
                result['matched'] = False
                result['reason'] = f"File operation failed: {e}"
                matched_results.remove(result)
                unmatched_results.append(result)

    return matched_results, unmatched_results


def save_unmatched(unmatched: List[dict], unmatched_dir: Path, dry_run: bool = False):
    if not unmatched:
        return
    if not dry_run:
        unmatched_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nSaving {len(unmatched)} unmatched file(s) to: {unmatched_dir}")
    for result in unmatched:
        dest = unmatched_dir / result['pdf_path'].name
        counter = 1
        while dest.exists():
            dest = unmatched_dir / f"{result['pdf_path'].stem}_{counter}{result['pdf_path'].suffix}"
            counter += 1
        if not dry_run:
            try:
                shutil.copy2(result['pdf_path'], dest)
            except Exception as e:
                print(f"  [ERROR] Failed to copy {result['pdf_path'].name}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Rename PDF invoices by matching Variable Symbol from PDF to MDB (FA table) and using cislo, firma, doklad",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # IN-PLACE rename (modifies originals!)
  python3 rename_pdfs_varsym.py --mdb export.mdb --source ./pdfs --in-place

  # Copy to output folder (safe)
  python3 rename_pdfs_varsym.py --mdb export.mdb --source ./pdfs --output ./renamed

  # Dry run to preview
  python3 rename_pdfs_varsym.py --mdb export.mdb --source ./pdfs --in-place --dry-run

  # Use folder + pattern to auto-select newest MDB file
  python3 rename_pdfs_varsym.py --mdb-folder ./exports --mdb-pattern "export" --source ./pdfs --output ./renamed

Workflow:
  1. Extract "Variabilní symbol" / "Variable Symbol" / "VS" from PDF text
  2. Find FA* table in MDB (POHODA invoices table)
  3. Match PDF's variable symbol to MDB's 'varsym' column
  4. Get corresponding 'cislo', 'firma', 'doklad' from same row
  5. Rename PDF to: {cislo}_{firma}_{doklad}.pdf

Requires: mdbtools (mdb-export, mdb-tables) - NOT pyodbc
        """
    )
    # MDB selection: either direct file path OR folder + pattern
    mdb_group = parser.add_mutually_exclusive_group(required=True)
    mdb_group.add_argument('--mdb', type=Path, help='Direct path to POHODA Access database export (.mdb)')
    mdb_group.add_argument('--mdb-folder', type=Path, help='Folder containing .mdb files (use with --mdb-pattern)')

    parser.add_argument('--mdb-pattern', type=str, help='Partial filename to match (case-insensitive). Selects newest matching .mdb file in --mdb-folder')
    parser.add_argument('--source', required=True, type=Path, help='Folder containing source PDF files')
    parser.add_argument('--output', type=Path, help='Output folder (copy mode). Required unless --in-place used.')
    parser.add_argument('--in-place', action='store_true',
                        help='Rename files in-place in source folder (no copy). Modifies originals!')
    parser.add_argument('--unmatched-dir', type=Path,
                        help='Folder for unmatched files (default: <source>/_unmatched)')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without modifying files')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')

    args = parser.parse_args()

    # Resolve MDB path
    if args.mdb_folder:
        if not args.mdb_pattern:
            print("Error: --mdb-pattern is required when using --mdb-folder", file=sys.stderr)
            return 1
        if not args.mdb_folder.exists():
            print(f"Error: MDB folder not found: {args.mdb_folder}", file=sys.stderr)
            return 1
        if not args.mdb_folder.is_dir():
            print(f"Error: MDB path is not a directory: {args.mdb_folder}", file=sys.stderr)
            return 1

        mdb_path = find_newest_mdb(args.mdb_folder, args.mdb_pattern)
        if not mdb_path:
            print(f"Error: No .mdb file matching '{args.mdb_pattern}' found in {args.mdb_folder}", file=sys.stderr)
            return 1
        print(f"Auto-selected newest MDB: {mdb_path}")
    else:
        mdb_path = args.mdb

    if not mdb_path.exists():
        print(f"Error: MDB file not found: {mdb_path}", file=sys.stderr)
        return 1
    if not args.source.exists():
        print(f"Error: Source folder not found: {args.source}", file=sys.stderr)
        return 1
    if not args.source.is_dir():
        print(f"Error: Source is not a directory: {args.source}", file=sys.stderr)
        return 1
    if not args.in_place and not args.output:
        print("Error: Either --output or --in-place must be specified", file=sys.stderr)
        return 1
    if args.in_place and args.output:
        print("Error: Cannot use both --in-place and --output", file=sys.stderr)
        return 1

    if args.in_place:
        output_dir = args.source
        copy_mode = False
        print("[WARN] IN-PLACE MODE: Files will be RENAMED in source folder!")
    else:
        output_dir = args.output
        copy_mode = True
        output_dir.mkdir(parents=True, exist_ok=True)

    unmatched_dir = args.unmatched_dir or (args.source / "_unmatched")

    try:
        varsym_to_data, table_name = load_varsym_to_cislo_map(mdb_path)
    except Exception as e:
        print(f"Error loading MDB: {e}", file=sys.stderr)
        return 1

    if not varsym_to_data:
        print("No varsym mappings found in MDB.")
        return 1

    print(f"\n{'='*60}")
    print(f"Processing PDFs from: {args.source}")
    print(f"Using MDB table: {table_name}")
    print(f"MDB mappings loaded: {len(varsym_to_data)}")
    print(f"Output mode: {'COPY to ' + str(output_dir) if copy_mode else 'RENAME in-place'}")
    if args.dry_run:
        print("DRY RUN - No files will be modified")
    print(f"{'='*60}")

    matched, unmatched = process_pdfs(
        source_dir=args.source,
        output_dir=output_dir,
        varsym_to_data=varsym_to_data,
        copy_mode=copy_mode,
        unmatched_dir=unmatched_dir,
        dry_run=args.dry_run,
    )

    save_unmatched(unmatched, unmatched_dir, dry_run=args.dry_run)

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Total PDFs processed: {len(matched) + len(unmatched)}")
    print(f"Successfully matched: {len(matched)}")
    print(f"Unmatched (need review): {len(unmatched)}")

    if matched:
        print(f"\n[OK] MATCHED FILES:")
        for r in matched:
            print(f"  {r['pdf_path'].name} -> {r['new_filename']}")
            print(f"     Varsym: {r['pdf_varsym']} -> Cislo: {r['cislo']}, Firma: {r.get('firma', 'N/A')}, Doklad: {r.get('doklad', 'N/A')}")

    if unmatched:
        print(f"\n[WARN] UNMATCHED FILES (copied to {unmatched_dir}):")
        for r in unmatched:
            print(f"  {r['pdf_path'].name}")
            print(f"     Reason: {r['reason']}")
            if 'pdf_varsym' in r:
                print(f"     PDF Varsym: {r['pdf_varsym']}")

    if args.dry_run:
        print("\n(Dry run - no files were actually modified)")

    return 0 if len(unmatched) == 0 else 1


# ============================================================
# GUI
# ============================================================
GUI_AVAILABLE = True
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    GUI_AVAILABLE = False


class PDFRenamerGUI:
    """GUI for the POHODA PDF Renamer."""

    def __init__(self, root):
        self.root = root
        self.root.title("POHODA PDF Renamer")
        self.root.geometry("1000x750")
        self.root.minsize(800, 600)

        # State variables
        self.mdb_path = tk.StringVar()
        self.mdb_folder = tk.StringVar()
        self.mdb_pattern = tk.StringVar()
        self.mdb_mode = tk.StringVar(value="file")  # "file" or "folder"
        self.source_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.unmatched_path = tk.StringVar()
        self.in_place = tk.BooleanVar(value=False)
        self.dry_run = tk.BooleanVar(value=False)
        self.verbose = tk.BooleanVar(value=False)

        self.processing = False
        self.log_queue = queue.Queue()

        self._build_ui()
        self._check_queue()

    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(main_frame, text="POHODA PDF Renamer", font=("TkDefaultFont", 14, "bold"))
        title_label.pack(pady=(0, 10))

        # Notebook for tabs
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        main_tab = ttk.Frame(notebook, padding=10)
        notebook.add(main_tab, text="Main")

        settings_tab = ttk.Frame(notebook, padding=10)
        notebook.add(settings_tab, text="Settings")

        log_tab = ttk.Frame(notebook, padding=10)
        notebook.add(log_tab, text="Log")

        self._build_main_tab(main_tab)
        self._build_settings_tab(settings_tab)
        self._build_log_tab(log_tab)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=5)

    def _build_main_tab(self, parent):
        # File selection frame
        file_frame = ttk.LabelFrame(parent, text="File Selection", padding=10)
        file_frame.pack(fill=tk.X, pady=(0, 10))

        # MDB selection mode
        mdb_mode_frame = ttk.Frame(file_frame)
        mdb_mode_frame.grid(row=0, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Label(mdb_mode_frame, text="MDB Selection:").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(mdb_mode_frame, text="Direct File", variable=self.mdb_mode, value="file",
                       command=self._on_mdb_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mdb_mode_frame, text="Folder + Pattern (auto-select newest)", variable=self.mdb_mode, value="folder",
                       command=self._on_mdb_mode_change).pack(side=tk.LEFT, padx=5)

        # MDB direct file (row 1)
        self.mdb_file_label = ttk.Label(file_frame, text="MDB Export:")
        self.mdb_file_label.grid(row=1, column=0, sticky=tk.W, pady=5)
        self.mdb_file_entry = ttk.Entry(file_frame, textvariable=self.mdb_path, width=60)
        self.mdb_file_entry.grid(row=1, column=1, sticky=tk.EW, padx=5)
        self.mdb_file_browse = ttk.Button(file_frame, text="Browse", command=lambda: self._browse_file(self.mdb_path, "MDB files", "*.mdb"))
        self.mdb_file_browse.grid(row=1, column=2, padx=5)

        # MDB folder + pattern (row 2-3)
        self.mdb_folder_label = ttk.Label(file_frame, text="MDB Folder:")
        self.mdb_folder_label.grid(row=2, column=0, sticky=tk.W, pady=5)
        self.mdb_folder_entry = ttk.Entry(file_frame, textvariable=self.mdb_folder, width=60)
        self.mdb_folder_entry.grid(row=2, column=1, sticky=tk.EW, padx=5)
        self.mdb_folder_browse = ttk.Button(file_frame, text="Browse", command=lambda: self._browse_dir(self.mdb_folder))
        self.mdb_folder_browse.grid(row=2, column=2, padx=5)

        self.mdb_pattern_label = ttk.Label(file_frame, text="ICO:")
        self.mdb_pattern_label.grid(row=3, column=0, sticky=tk.W, pady=5)
        self.mdb_pattern_entry = ttk.Entry(file_frame, textvariable=self.mdb_pattern, width=60)
        self.mdb_pattern_entry.grid(row=3, column=1, sticky=tk.EW, padx=5)
        ttk.Label(file_frame, text="(case-insensitive, selects newest match)").grid(row=3, column=2, sticky=tk.W, padx=5)

        # Auto-refresh button
        self.mdb_refresh_btn = ttk.Button(file_frame, text="Find Newest", command=self._find_newest_mdb)
        self.mdb_refresh_btn.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)

        # Source folder
        ttk.Label(file_frame, text="Source PDFs:").grid(row=5, column=0, sticky=tk.W, pady=5)
        ttk.Entry(file_frame, textvariable=self.source_path, width=60).grid(row=5, column=1, sticky=tk.EW, padx=5)
        ttk.Button(file_frame, text="Browse", command=lambda: self._browse_dir(self.source_path)).grid(row=5, column=2, padx=5)

        # Output folder
        ttk.Label(file_frame, text="Output Folder:").grid(row=6, column=0, sticky=tk.W, pady=5)
        self.output_entry = ttk.Entry(file_frame, textvariable=self.output_path, width=60)
        self.output_entry.grid(row=6, column=1, sticky=tk.EW, padx=5)
        self.output_browse = ttk.Button(file_frame, text="Browse", command=lambda: self._browse_dir(self.output_path))
        self.output_browse.grid(row=6, column=2, padx=5)

        # Unmatched folder
        ttk.Label(file_frame, text="Unmatched Folder:").grid(row=7, column=0, sticky=tk.W, pady=5)
        ttk.Entry(file_frame, textvariable=self.unmatched_path, width=60).grid(row=7, column=1, sticky=tk.EW, padx=5)
        ttk.Button(file_frame, text="Browse", command=lambda: self._browse_dir(self.unmatched_path)).grid(row=7, column=2, padx=5)

        file_frame.columnconfigure(1, weight=1)

        # Initialize mode
        self._on_mdb_mode_change()

        # Mode selection
        mode_frame = ttk.LabelFrame(parent, text="Mode", padding=10)
        mode_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Radiobutton(mode_frame, text="Copy to Output Folder (Safe)", variable=self.in_place, value=False,
                       command=self._on_mode_change).pack(anchor=tk.W)
        ttk.Radiobutton(mode_frame, text="Rename In-Place (Modifies Originals!)", variable=self.in_place, value=True,
                       command=self._on_mode_change).pack(anchor=tk.W)

        # Options
        options_frame = ttk.LabelFrame(parent, text="Options", padding=10)
        options_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Checkbutton(options_frame, text="Dry Run (Preview only)", variable=self.dry_run).pack(anchor=tk.W)
        ttk.Checkbutton(options_frame, text="Verbose Output", variable=self.verbose).pack(anchor=tk.W)

        # Progress
        progress_frame = ttk.LabelFrame(parent, text="Progress", padding=10)
        progress_frame.pack(fill=tk.X, pady=(0, 10))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=5)

        self.progress_label = ttk.Label(progress_frame, text="Waiting to start...")
        self.progress_label.pack(anchor=tk.W)

        # Buttons
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill=tk.X, pady=10)

        self.start_button = ttk.Button(button_frame, text="Start Processing", command=self._start_processing, style="Accent.TButton")
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(button_frame, text="Stop", command=self._stop_processing, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT, padx=5)

        # Results summary
        summary_frame = ttk.LabelFrame(parent, text="Summary", padding=10)
        summary_frame.pack(fill=tk.BOTH, expand=True)

        self.summary_text = scrolledtext.ScrolledText(summary_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.summary_text.pack(fill=tk.BOTH, expand=True)

    def _build_settings_tab(self, parent):
        info_frame = ttk.LabelFrame(parent, text="How It Works", padding=10)
        info_frame.pack(fill=tk.BOTH, expand=True)

        info_text = """POHODA PDF Renamer - Variable Symbol Matching

WORKFLOW:
1. Scans ALL pages of each PDF for variable symbols
2. Finds FA* table in MDB (POHODA invoices table)
3. Matches PDF's variable symbol to MDB's 'varsym' column
4. Gets corresponding 'cislo', 'firma', 'doklad' from same row
5. Renames PDF to: {cislo}_{firma}_{doklad}.pdf

SUPPORTED VARIABLE SYMBOL FORMATS (in PDF):
• Variabilní symbol: 123456789
• Variabilni symbol: 123456789
• Variabilný symbol: 123456789
• Variabilny symbol: 123456789
• Variable Symbol: 123456789
• VS: 123456789 / V.S.: 123456789 / VS 123456789
• Var. symb.: 123456789 / Var. symbol: 123456789
• Variabilni 123456789 / Variable 123456789 / Var 123456789
• OCR variants: simbolo, simbol, símbolo
• With separators: 123/456/789, 123-456-789

MDB REQUIREMENTS:
• Table starting with 'FA' (e.g., FA, Faktury, FA_2024)
• Column 'varsym' (variable symbol)
• Column 'cislo' (document number)
• Column 'firma' (supplier/company name) - optional
• Column 'doklad' (document type) - optional

REQUIREMENTS:
• mdbtools (mdb-export, mdb-tables)
  Arch: sudo pacman -S mdbtools
  Debian/Ubuntu: sudo apt install mdbtools
  Fedora: sudo dnf install mdbtools
  macOS: brew install mdbtools
• Python: pip install pandas pypdf
"""

        text_widget = scrolledtext.ScrolledText(info_frame, wrap=tk.WORD)
        text_widget.pack(fill=tk.BOTH, expand=True)
        text_widget.insert(tk.END, info_text)
        text_widget.config(state=tk.DISABLED)

    def _build_log_tab(self, parent):
        self.log_text = scrolledtext.ScrolledText(parent, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _on_mode_change(self):
        if self.in_place.get():
            self.output_entry.config(state=tk.DISABLED)
            self.output_browse.config(state=tk.DISABLED)
            self.output_path.set("")
        else:
            self.output_entry.config(state=tk.NORMAL)
            self.output_browse.config(state=tk.NORMAL)

    def _on_mdb_mode_change(self):
        mode = self.mdb_mode.get()
        if mode == "file":
            # Show file selection, hide folder+pattern
            self.mdb_file_label.grid()
            self.mdb_file_entry.grid()
            self.mdb_file_browse.grid()
            self.mdb_folder_label.grid_remove()
            self.mdb_folder_entry.grid_remove()
            self.mdb_folder_browse.grid_remove()
            self.mdb_pattern_label.grid_remove()
            self.mdb_pattern_entry.grid_remove()
            self.mdb_refresh_btn.grid_remove()
        else:
            # Show folder+pattern, hide file selection
            self.mdb_file_label.grid_remove()
            self.mdb_file_entry.grid_remove()
            self.mdb_file_browse.grid_remove()
            self.mdb_folder_label.grid()
            self.mdb_folder_entry.grid()
            self.mdb_folder_browse.grid()
            self.mdb_pattern_label.grid()
            self.mdb_pattern_entry.grid()
            self.mdb_refresh_btn.grid()

    def _find_newest_mdb(self):
        folder = self.mdb_folder.get()
        pattern = self.mdb_pattern.get()
        if not folder or not pattern:
            messagebox.showwarning("Missing Info", "Please select a folder and enter a pattern first.")
            return
        mdb_path = find_newest_mdb(Path(folder), pattern)
        if mdb_path:
            self.mdb_path.set(str(mdb_path))
            self._log(f"Found newest MDB: {mdb_path}", "SUCCESS")
        else:
            messagebox.showinfo("Not Found", f"No .mdb file matching '{pattern}' found in {folder}")
            self._log(f"No .mdb file matching '{pattern}' found in {folder}", "WARN")

    def _browse_file(self, var, title, filetypes):
        path = filedialog.askopenfilename(title=title, filetypes=[(title, filetypes), ("All files", "*.*")])
        if path:
            var.set(path)

    def _browse_dir(self, var):
        path = filedialog.askdirectory(title="Select Folder")
        if path:
            var.set(path)

    def _log(self, message, level="INFO"):
        timestamp = pd.Timestamp.now().strftime("%H:%M:%S")
        self.log_queue.put((f"[{timestamp}] {message}", level))

    def _log_to_widget(self, message, level="INFO"):
        self.log_text.config(state=tk.NORMAL)
        # Color coding for different levels
        if level == "ERROR":
            self.log_text.insert(tk.END, message + "\n", "error")
        elif level == "WARN":
            self.log_text.insert(tk.END, message + "\n", "warn")
        elif level == "SUCCESS":
            self.log_text.insert(tk.END, message + "\n", "success")
        else:
            self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        # Configure tags
        self.log_text.tag_config("error", foreground="red")
        self.log_text.tag_config("warn", foreground="orange")
        self.log_text.tag_config("success", foreground="green")

    def _update_summary(self, message):
        self.summary_text.config(state=tk.NORMAL)
        self.summary_text.insert(tk.END, message + "\n")
        self.summary_text.see(tk.END)
        self.summary_text.config(state=tk.DISABLED)

    def _check_queue(self):
        try:
            while True:
                message, level = self.log_queue.get_nowait()
                self._log_to_widget(message, level)
        except queue.Empty:
            pass
        self.root.after(100, self._check_queue)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

        self.summary_text.config(state=tk.NORMAL)
        self.summary_text.delete(1.0, tk.END)
        self.summary_text.config(state=tk.DISABLED)

    def _validate_inputs(self):
        if not self.mdb_path.get():
            messagebox.showerror("Error", "Please select an MDB file")
            return False
        if not Path(self.mdb_path.get()).exists():
            messagebox.showerror("Error", "MDB file does not exist")
            return False
        if not self.source_path.get():
            messagebox.showerror("Error", "Please select a source folder")
            return False
        if not Path(self.source_path.get()).exists():
            messagebox.showerror("Error", "Source folder does not exist")
            return False
        if not self.in_place.get() and not self.output_path.get():
            messagebox.showerror("Error", "Please select an output folder (or enable In-Place mode)")
            return False
        if self.in_place.get():
            result = messagebox.askyesno("Confirm In-Place Mode",
                "This will RENAME files in the source folder. Original names will be lost.\nContinue?")
            if not result:
                return False
        return True

    def _start_processing(self):
        if not self._validate_inputs():
            return

        self.processing = True
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.progress_var.set(0)
        self.progress_label.config(text="Starting...")
        self.status_var.set("Processing...")
        self._clear_log()

        # Run in background thread
        thread = threading.Thread(target=self._process_thread, daemon=True)
        thread.start()

    def _stop_processing(self):
        self.processing = False
        self._log("Stop requested...", "WARN")
        self.status_var.set("Stopping...")

    def _process_thread(self):
        try:
            self.log_queue.put(("Loading MDB...", "INFO"))

            mdb_path = Path(self.mdb_path.get())
            source_dir = Path(self.source_path.get())

            if self.in_place.get():
                output_dir = source_dir
                copy_mode = False
            else:
                output_dir = Path(self.output_path.get())
                copy_mode = True
                output_dir.mkdir(parents=True, exist_ok=True)

            unmatched_dir = Path(self.unmatched_path.get()) if self.unmatched_path.get() else (source_dir / "_unmatched")

            varsym_to_data, table_name = load_varsym_to_cislo_map(mdb_path)

            if not varsym_to_data:
                self.log_queue.put(("No varsym mappings found in MDB.", "ERROR"))
                return

            self.log_queue.put((f"Loaded {len(varsym_to_data)} mappings from table '{table_name}'.", "INFO"))

            pdf_files = list(source_dir.rglob("*.pdf"))
            if not pdf_files:
                self.log_queue.put(("No PDF files found.", "WARN"))
                return

            self.log_queue.put((f"Found {len(pdf_files)} PDF file(s) to process.", "INFO"))

            matched_results = []
            unmatched_results = []

            for i, pdf_path in enumerate(pdf_files):
                if not self.processing:
                    break

                progress = int((i / len(pdf_files)) * 100)
                self.progress_var.set(progress)
                self.progress_label.config(text=f"Processing {i+1}/{len(pdf_files)}: {pdf_path.name}")

                self.log_queue.put((f"\nProcessing: {pdf_path.name}", "INFO"))

                text = extract_text_from_pdf(pdf_path)
                if text.startswith("[ERROR"):
                    result = {'pdf_path': pdf_path, 'matched': False, 'reason': text}
                    unmatched_results.append(result)
                    self.log_queue.put((f"  ✗ {text}", "ERROR"))
                    continue

                # Extract variable symbol from PDF
                pdf_varsym = extract_variable_symbol(text)

                if not pdf_varsym:
                    result = {'pdf_path': pdf_path, 'matched': False, 'reason': "No variable symbol found in PDF"}
                    unmatched_results.append(result)
                    self.log_queue.put(("  ⚠ No variable symbol found in PDF", "WARN"))
                    # Show debug candidates if verbose
                    if self.verbose.get():
                        candidates = extract_all_variable_symbol_candidates(text)
                        if candidates:
                            self.log_queue.put((f"     Candidates: {[(c[1], c[0]) for c in candidates[:5]]}", "DEBUG"))
                    continue

                self.log_queue.put((f"  Found variable symbol: {pdf_varsym}", "INFO"))

                # Look up in MDB mapping
                data = varsym_to_data.get(pdf_varsym)

                if not data:
                    result = {'pdf_path': pdf_path, 'matched': False, 'reason': f"Variable symbol '{pdf_varsym}' not found in MDB", 'pdf_varsym': pdf_varsym}
                    unmatched_results.append(result)
                    self.log_queue.put((f"  ⚠ Variable symbol '{pdf_varsym}' not found in MDB", "WARN"))
                    continue

                cislo, firma, doklad = data

                # Create new filename: cislo_firma_doklad.pdf (skip empty parts)
                parts = [sanitize_filename(cislo)]
                if firma:
                    parts.append(sanitize_filename(firma))
                if doklad:
                    parts.append(sanitize_filename(doklad))
                new_name = "_".join(parts) + ".pdf"

                result = {
                    'pdf_path': pdf_path,
                    'matched': True,
                    'cislo': cislo,
                    'firma': firma,
                    'doklad': doklad,
                    'pdf_varsym': pdf_varsym,
                    'new_filename': new_name,
                    'reason': f"Matched varsym '{pdf_varsym}' -> cislo '{cislo}', firma '{firma}', doklad '{doklad}'",
                }
                matched_results.append(result)

                self.log_queue.put((f"  ✓ {result['reason']}", "SUCCESS"))
                self.log_queue.put((f"     New name: {new_name}", "INFO"))

                if not self.dry_run.get():
                    dest_path = output_dir / new_name
                    counter = 1
                    original_dest = dest_path
                    while dest_path.exists():
                        stem = original_dest.stem
                        suffix = original_dest.suffix
                        dest_path = output_dir / f"{stem}_{counter}{suffix}"
                        counter += 1

                    try:
                        if copy_mode:
                            shutil.copy2(pdf_path, dest_path)
                            self.log_queue.put((f"     → Copied to {dest_path.name}", "INFO"))
                        else:
                            shutil.move(str(pdf_path), str(dest_path))
                            self.log_queue.put((f"     → Renamed to {dest_path.name}", "INFO"))
                    except Exception as e:
                        self.log_queue.put((f"     ✗ Error: {e}", "ERROR"))
                        result['matched'] = False
                        result['reason'] = f"File operation failed: {e}"
                        matched_results.remove(result)
                        unmatched_results.append(result)

            # Save unmatched
            if unmatched_results:
                save_unmatched(unmatched_results, unmatched_dir, dry_run=self.dry_run.get())
                self.log_queue.put((f"\nSaved {len(unmatched_results)} unmatched file(s) to: {unmatched_dir}", "INFO"))

            # Summary
            self.progress_var.set(100)
            self.progress_label.config(text="Complete")

            self.log_queue.put((f"\n{'='*60}", "INFO"))
            self.log_queue.put(("SUMMARY", "INFO"))
            self.log_queue.put((f"{'='*60}", "INFO"))
            self.log_queue.put((f"Total PDFs processed: {len(matched_results) + len(unmatched_results)}", "INFO"))
            self.log_queue.put((f"Successfully matched: {len(matched_results)}", "SUCCESS"))
            self.log_queue.put((f"Unmatched (need review): {len(unmatched_results)}", "WARN" if unmatched_results else "INFO"))

            if matched_results:
                self.log_queue.put(("\n✓ MATCHED FILES:", "SUCCESS"))
                for r in matched_results:
                    self.log_queue.put((f"  {r['pdf_path'].name} → {r['new_filename']}", "INFO"))
                    self.log_queue.put((f"     Varsym: {r['pdf_varsym']} → Cislo: {r['cislo']}, Firma: {r.get('firma', 'N/A')}, Doklad: {r.get('doklad', 'N/A')}", "INFO"))

            if unmatched_results:
                self.log_queue.put((f"\n⚠ UNMATCHED FILES (copied to {unmatched_dir}):", "WARN"))
                for r in unmatched_results:
                    self.log_queue.put((f"  {r['pdf_path'].name}", "WARN"))
                    self.log_queue.put((f"     Reason: {r['reason']}", "WARN"))
                    if 'pdf_varsym' in r:
                        self.log_queue.put((f"     PDF Varsym: {r['pdf_varsym']}", "WARN"))

            if self.dry_run.get():
                self.log_queue.put(("\n(Dry run - no files were actually modified)", "INFO"))

            # Update summary tab
            summary = f"Matched: {len(matched_results)} | Unmatched: {len(unmatched_results)} | Total: {len(matched_results) + len(unmatched_results)}"
            self.root.after(0, lambda: self._update_summary(summary))

        except Exception as e:
            self.log_queue.put((f"Error: {e}", "ERROR"))
            import traceback
            self.log_queue.put((traceback.format_exc(), "ERROR"))
        finally:
            self.processing = False
            self.root.after(0, self._processing_finished)

    def _processing_finished(self):
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_var.set("Ready")
        if not self.processing:
            self.progress_label.config(text="Done")


def run_gui():
    """Launch the GUI application."""
    if not GUI_AVAILABLE:
        print("GUI not available. tkinter is required.", file=sys.stderr)
        sys.exit(1)

    root = tk.Tk()

    # Try to set a nice theme
    try:
        style = ttk.Style()
        style.theme_use('clam')
    except:
        pass

    app = PDFRenamerGUI(root)
    root.mainloop()


if __name__ == '__main__':
    # Check for --gui argument before parsing other args
    if '--gui' in sys.argv:
        run_gui()
    else:
        sys.exit(main())