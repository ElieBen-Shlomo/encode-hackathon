import os
import openpyxl

out_path = os.environ["OUT_XLSX"]

wb = openpyxl.load_workbook(out_path)
ws = wb["Data"]

# Range affected: U3:AI20. Any cell whose content is not purely numeric
# (digits 0-9, optionally with a sign/decimal point) gets replaced with 0.
for row in ws["U3:AI20"]:
    for cell in row:
        value = cell.value
        if isinstance(value, (int, float)):
            continue
        text = "" if value is None else str(value).strip()
        is_numeric = False
        if text:
            try:
                float(text)
                is_numeric = True
            except ValueError:
                is_numeric = False
        if not is_numeric:
            cell.value = 0

wb.save(out_path)
