from typing import Dict, Any

import pandas as pd

from app.damapql.dch.dtypes import AttributeTablePayload


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
    def getColGenericType(series_or_dtype):
        """
        Backward-compatible wrapper:
        - pd.Series -> infer from values
        - str dtype/db type -> infer from type string
        """
        if isinstance(series_or_dtype, pd.Series):
            return ManageDataFrameUtils.getColGenericTypeFromSeries(series_or_dtype)

        if isinstance(series_or_dtype, str):
            return ManageDataFrameUtils.getColGenericTypeFromString(series_or_dtype)

        return "string"

    @staticmethod
    def getColGenericTypeFromSeries(series: pd.Series):
        if pd.api.types.is_integer_dtype(series) or pd.api.types.is_float_dtype(series):
            return "number"

        if pd.api.types.is_datetime64_any_dtype(series):
            return "date"

        non_null = series.dropna()
        if non_null.empty:
            return "string"

        sample = non_null.iloc[0]

        if isinstance(sample, dict):
            return "object"
        if isinstance(sample, (list, tuple)):
            return "array"
        if isinstance(sample, (int, float)):
            return "number"

        return "string"

    @staticmethod
    def getColGenericTypeFromString(d_type: str):
        d = d_type.lower()

        if any(x in d for x in ["int", "numeric", "decimal", "real", "double", "float", "serial"]):
            return "number"

        if any(x in d for x in ["date", "time"]):
            return "date"

        if any(x in d for x in ["json", "jsonb", "dict"]):
            return "object"

        if any(x in d for x in ["array"]):
            return "array"

        return "string"

    @classmethod
    def grid_response(cls, df: pd.DataFrame, pkCols=())->AttributeTablePayload :
        columns = [
            {
                "id": col,
                "label": cls.create_label(col.title()),
                "type": cls.getColGenericType(df[col]),
                "disablePadding": False,
            }
            for col in df.columns.tolist()
        ]

        df = df.round(2)

        # replace NaN with None, preserving object/list/dict semantics
        df = df.where(pd.notnull(df), None)
        return AttributeTablePayload(**{
                "pkCols": pkCols,
                "columns": columns,
                "rows": df.to_dict(orient="records"),
            })

