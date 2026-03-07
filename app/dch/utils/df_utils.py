from typing import Dict

import pandas as pd

from app.dch.dtypes import GridPayload


class ManageDataFrameUtils:
    @staticmethod
    def create_label(value):
        if isinstance(value, str):
            value = value.upper()
            replace_char = ["_", "-"]
            for char in replace_char:
                value = value.replace(char, " ")

            return value
        if isinstance(value, tuple):
            res = []
            for v in value:
                if isinstance(v, str):
                    res.append(v.upper())
                else:
                    res.append(str(v).split(" ")[0])
            return "/".join(res)
        else:
            return str(value).split(" ")[0]

    @staticmethod
    def getColGenericType(d_type: str):
        number_types = ["int64", "float64"]
        date_types = ["datetime64"]
        if d_type in number_types:
            return "number"
        elif d_type in date_types:
            return "date"
        else:
            return "string"

    @classmethod
    def grid_response(cls, df: pd.DataFrame, pkCols=())-> Dict[str, GridPayload]:
        columns = [{"id": col,
                    "label": cls.create_label(col.title()),
                    "type": cls.getColGenericType(str(df[col].dtype)),
                    "disablePadding": False} for col in df.columns.tolist()]
        df = df.fillna('')
        df = df.round(2)
        return {"payload": {
            "pkCols": pkCols,
            "columns": columns,
            "rows": df.to_dict(orient='records')}}
