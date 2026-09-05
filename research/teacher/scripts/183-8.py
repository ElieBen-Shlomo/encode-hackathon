import os

import openpyxl

wb = openpyxl.load_workbook(os.environ["OUT_XLSX"])
ws = wb["Sheet1"]

# Weighted average per FY, weighted by generation (col C), matching FY in col B.
# J <- %PLF (D), K <- M/C Avail (E), L <- Grid Avail (F); col I already sums generation per FY.
for row in range(3, 7):
    for out_col, data_col in (("J", "D"), ("K", "E"), ("L", "F")):
        ws[f"{out_col}{row}"] = (
            f"=SUMPRODUCT(($B$3:$B$43=$H{row})*$C$3:$C$43,{data_col}$3:{data_col}$43)/$I{row}"
        )

wb.save(os.environ["OUT_XLSX"])
