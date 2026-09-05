"""Strip every non-numeric character (currency symbols, commas, letters, etc.) from each
cell in Sheet1!A1:H1 while keeping digits and the decimal point, then store the result as
a real number."""
import os
import re

import openpyxl

wb = openpyxl.load_workbook(os.environ["OUT_XLSX"])
ws = wb["Sheet1"]

for cell in ws["A1:H1"][0]:
    val = cell.value
    if isinstance(val, (int, float)):
        cell.value = float(val)
    elif isinstance(val, str):
        cleaned = re.sub(r"[^0-9.]", "", val)
        if cleaned not in ("", "."):
            cell.value = float(cleaned)

wb.save(os.environ["OUT_XLSX"])
