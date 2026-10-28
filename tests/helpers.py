"""Small builders shared by the test modules.

``write_xlsx`` used to live in ``tests/test_backfill.py``, so ``test_sans.py``
imported one test module from another.  SPEC-039 removed the workbook reader
from ``app/backfill.py``; the builder stays here because one regression test
still needs to prove that a ``.xlsx`` player import is refused.
"""

from zipfile import ZipFile

SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def write_xlsx(path, rows):
    """Write a minimal XLSX: text cells are shared strings, numbers are values."""
    shared, body = [], []
    for index, row in enumerate(rows, 1):
        cells = []
        for position, value in enumerate(row):
            if value is None or value == '':
                continue
            column = chr(ord('A') + position)
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{column}{index}"><v>{value}</v></c>')
            else:
                if value not in shared:
                    shared.append(value)
                cells.append(f'<c r="{column}{index}" t="s"><v>{shared.index(value)}</v></c>')
        body.append(f'<row r="{index}">' + ''.join(cells) + '</row>')
    with ZipFile(path, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml',
                         f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{SHEET_NS}">'
                         + ''.join(f'<si><t>{value}</t></si>' for value in shared) + '</sst>')
        archive.writestr('xl/worksheets/sheet1.xml',
                         f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{SHEET_NS}">'
                         '<sheetData>' + ''.join(body) + '</sheetData></worksheet>')
    return path