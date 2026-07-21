import os
import pandas as pd
from tabulate import tabulate
import json
import traceback
import inspect
from datetime import datetime
from typing import Any, Dict, Optional, Literal



# pd.set_option('display.max_columns', None)
# pd.set_option('display.expand_frame_repr', False)
# pd.set_option('display.width', 1000)
TableFormat = Literal[
    "plain",
    "simple",
    "github",
    "grid",
    "fancy_grid",
    "pipe",
    "orgtbl",
    "jira",
    "presto",
    "pretty",
    "psql",
    "rst",
    "mediawiki",
    "moinmoin",
    "youtrack",
    "html",
    "unsafehtml",
    "latex",
    "latex_raw",
    "latex_booktabs",
    "latex_longtable",
    "tsv",
    "csv",
]
class DALogger:
    ANSI_COLORS = {
        "DEBUG": "\033[96m",
        "INFO": "\033[94m",
        "SUCCESS": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
        "RESET": "\033[0m",
    }

    LEVEL_ORDER = {
        "DEBUG": 10,
        "INFO": 20,
        "SUCCESS": 25,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }

    def __init__(
        self,
        name: str = "DALogger",
        enabled: bool = True,
        trim_length: int = 1000,
        min_level: str = "DEBUG",
        log_to_file: bool = False,
        log_file_path: str = "app.log",
        show_location_default: bool = True,
        json_mode: bool = False,
    ):
        self.name = name
        self.enabled = enabled
        self.trim_length = trim_length
        self.min_level = min_level.upper()
        self.log_to_file = log_to_file
        self.log_file_path = log_file_path
        self.show_location_default = show_location_default
        self.json_mode = json_mode

    def _should_log(self, level: str) -> bool:
        if not self.enabled:
            return False
        return self.LEVEL_ORDER.get(level, 0) >= self.LEVEL_ORDER.get(self.min_level, 0)

    @staticmethod
    def _message_to_str(message: Any) -> object | str:
        try:
            if hasattr(message, "model_dump_json") and callable(message.model_dump_json):
                return message.model_dump_json()

            if hasattr(message, "model_dump") and callable(message.model_dump):
                return json.dumps(message.model_dump(), ensure_ascii=False, default=str)

            if isinstance(message, (dict, list, tuple, set)):
                return json.dumps(message, ensure_ascii=False, default=str)

            if isinstance(message, (bytes, bytearray)):
                return message.decode("utf-8", errors="replace")

            return str(message)
        except Exception:
            return "<unprintable>"

    def _combine_messages(self, messages: tuple[Any, ...]) -> str:
        if not messages:
            return ""
        return " ".join(self._message_to_str(m) for m in messages)

    def _safe_str(self, value: Any, max_len: int = 120) -> str:
        s = self._message_to_str(value)
        if len(s) > max_len:
            s = s[:max_len] + "..."
        return s

    def _normalize_extra(self, extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not extra:
            return {}
        normalized = {}
        for k, v in extra.items():
            try:
                json.dumps(v, default=str)
                normalized[k] = v
            except Exception:
                normalized[k] = self._safe_str(v)
        return normalized

    def _format_extra_text(self, extra: Dict[str, Any]) -> str:
        if not extra:
            return ""
        parts = [f"{k}={self._safe_str(v)}" for k, v in sorted(extra.items())]
        return " | " + " ".join(parts)

    def _get_location(self) -> str:
        try:
            for frame_info in inspect.stack()[3:]:
                if frame_info.function in {
                    "_log",
                    "debug",
                    "info",
                    "success",
                    "warning",
                    "error",
                    "critical",
                    "exception",
                }:
                    continue

                filename = os.path.basename(frame_info.filename)
                return f"{filename}:{frame_info.lineno}"
        except Exception:
            pass
        return ""

    def _trim_message(self, msg: str, trim: bool) -> str:
        if trim and len(msg) > self.trim_length:
            return msg[: self.trim_length] + "..."
        return msg



    # def _make_record(
    #     self,
    #     *,
    #     level: str,
    #     label: str,
    #     message: str,
    #     extra: Optional[Dict[str, Any]],
    #     show_location: Optional[bool],
    #     trim: bool,
    # ) -> Dict[str, Any]:
    #     if show_location is None:
    #         show_location = self.show_location_default
    #
    #     msg_str = self._trim_message(message, trim)
    #     extra_dict = self._normalize_extra(extra)
    #     location = self._get_location() if show_location else ""
    #
    #     return {
    #         "timestamp": datetime.now().isoformat(timespec="seconds"),
    #         "logger": self.name,
    #         "level": level,
    #         "label": label,
    #         "location": location,
    #         "message": msg_str,
    #         "extra": extra_dict,
    #     }

    def _emit_console_text(self, record: Dict[str, Any]) -> None:
        color = self.ANSI_COLORS.get(record["level"], "")
        reset = self.ANSI_COLORS["RESET"]
        timestamp = datetime.now().strftime("[%H:%M:%S] ")
        location = f'{record["location"]} - ' if record["location"] else ""
        extra_str = self._format_extra_text(record["extra"])

        output = f'{color}{timestamp}[{record["label"]}] {location}{record["message"]}{extra_str}{reset}'
        try:
            print(output)
        except UnicodeEncodeError:
            print(f'{timestamp}[{record["label"]}] {location}{record["message"]}{extra_str}')

    def _emit_console_json(self, record: Dict[str, Any]) -> None:
        print(json.dumps(record, ensure_ascii=False, default=str))

    def _emit_file(self, record: Dict[str, Any]) -> None:
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _format_table_message(
            self,
            messages: tuple[Any, ...],
            tablefmt: str = "grid",
    ) -> str:
        if not messages:
            return ""

        formatted_parts = []

        for data in messages:
            # If it's a string, keep it as text
            if isinstance(data, str):
                formatted_parts.append(data)
                continue

            try:
                # Format DataFrames

                if isinstance(data, pd.DataFrame):
                    try:
                        import geopandas as gpd
                    except ImportError:
                        gpd = None
                    if gpd is not None and isinstance(data, gpd.GeoDataFrame):
                        geom_col = data.geometry.name
                        # Drop heavy coordinate geometries
                        log_df = pd.DataFrame(data.drop(columns=[geom_col], errors='ignore'))

                        # Extract the SRID string (e.g., "EPSG:4326")
                        srid_val = f"EPSG:{data.crs.to_epsg()}" if data.crs and data.crs.to_epsg() else str(data.crs)
                        log_df["srid"] = srid_val
                    else:
                        log_df = data
                    table_str = tabulate(
                        log_df.values.tolist(),
                        headers=list(log_df.columns),
                        tablefmt=tablefmt,
                        showindex=False,
                    )
                    formatted_parts.append("\n" + table_str)
                    continue

                # Format Series
                if isinstance(data, pd.Series):
                    df_series = data.reset_index()
                    table_str = tabulate(
                        df_series.values.tolist(),
                        headers=list(df_series.columns),
                        tablefmt=tablefmt,
                        showindex=False,
                    )
                    formatted_parts.append("\n" + table_str)
                    continue

                # Format Lists (of dicts/lists)
                if isinstance(data, list):
                    table_str = tabulate(data, headers="keys", tablefmt=tablefmt)
                    formatted_parts.append("\n" + table_str)
                    continue

                # Format Dictionaries
                if isinstance(data, dict):
                    rows = [{"Key": key, "Value": value} for key, value in data.items()]
                    table_str = tabulate(rows, headers="keys", tablefmt=tablefmt)
                    formatted_parts.append("\n" + table_str)
                    continue

            except Exception as e:
                # Fallback to standard string representation if tabulate fails
                print(f"[Logger Internal Error]: {e}")

            # Catch-all for any other data types mixed in
            formatted_parts.append(self._message_to_str(data))

        # Join everything together with spaces, cleaning up extra newlines gently
        return " ".join(formatted_parts).strip()

    # def _format_table_message(
    #         self,
    #         messages: tuple[Any, ...],
    #         tablefmt: str = "grid",
    # ) -> str:
    #     # try:
    #     #     from tabulate import tabulate
    #     # except Exception:
    #     #     return self._combine_messages(messages)
    #
    #     if not messages:
    #         return ""
    #
    #     if len(messages) == 1:
    #         data = messages[0]
    #
    #         try:
    #
    #             if isinstance(data, pd.DataFrame):
    #                 return tabulate(
    #                     data,
    #                     headers="keys",
    #                     tablefmt=tablefmt,
    #                     showindex=False,
    #                 )
    #
    #             if isinstance(data, pd.Series):
    #                 return tabulate(
    #                     data.reset_index(),
    #                     headers="keys",
    #                     tablefmt=tablefmt,
    #                     showindex=False,
    #                 )
    #
    #         except Exception:
    #             pass
    #
    #         if isinstance(data, list):
    #             return tabulate(
    #                 data,
    #                 headers="keys",
    #                 tablefmt=tablefmt,
    #             )
    #
    #         if isinstance(data, dict):
    #             rows = [
    #                 {"Key": key, "Value": value}
    #                 for key, value in data.items()
    #             ]
    #             return tabulate(
    #                 rows,
    #                 headers="keys",
    #                 tablefmt=tablefmt,
    #             )
    #
    #     return self._combine_messages(messages)

    # def _log(
    #     self,
    #     level: str,
    #     label: str,
    #     *messages: Any,
    #     extra: Optional[Dict[str, Any]] = None,
    #     show_location: Optional[bool] = None,
    #     trim: bool = True,
    #     as_table: bool = False,
    #     tablefmt: str = "grid"
    # ) -> None:
    #     if not self._should_log(level):
    #         return
    #
    #     combined_message = self._combine_messages(messages)
    #     record = self._make_record(
    #         level=level,
    #         label=label,
    #         message=combined_message,
    #         extra=extra,
    #         show_location=show_location,
    #         trim=trim,
    #     )
    #
    #     if self.json_mode:
    #         self._emit_console_json(record)
    #     else:
    #         self._emit_console_text(record)
    #
    #     if self.log_to_file:
    #         self._emit_file(record)

    def _make_record(
        self,
        *,
        level: str,
        label: str,
        message: str,
        extra: Optional[Dict[str, Any]],
        show_location: Optional[bool],
        trim: bool,
    ) -> Dict[str, Any]:
        if show_location is None:
            show_location = self.show_location_default

        # CRITICAL FIX: Only trim if trim is explicitly True
        msg_str = self._trim_message(message, trim) if trim else message
        extra_dict = self._normalize_extra(extra)
        location = self._get_location() if show_location else ""

        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "logger": self.name,
            "level": level,
            "label": label,
            "location": location,
            "message": msg_str,
            "extra": extra_dict,
        }

    def _log(
            self,
            level: str,
            label: str,
            *messages: Any,
            extra: Optional[Dict[str, Any]] = None,
            show_location: Optional[bool] = None,
            trim: bool = True,
            as_table: bool = False,
            tablefmt: str = "grid",
    ) -> None:
        if not self._should_log(level):
            return

        # Handle table generation here cleanly so ALL log methods can use it
        if as_table:
            combined_message = self._format_table_message(
                messages,
                tablefmt=tablefmt,
            )
            # FORCE trim to False so _make_record doesn't chop up your newly built table grid
            trim = False
        else:
            combined_message = self._combine_messages(messages)

        record = self._make_record(
            level=level,
            label=label,
            message=combined_message,
            extra=extra,
            show_location=show_location,
            trim=trim,
        )

        if self.json_mode:
            self._emit_console_json(record)
        else:
            self._emit_console_text(record)

        if self.log_to_file:
            self._emit_file(record)

    def debug(self, *messages: Any, extra: Optional[Dict[str, Any]] = None, trim: bool = False,
              as_table: bool = True, tablefmt: TableFormat = "simple"):
        self._log("DEBUG", "🐛 DEBUG", *messages, extra=extra, show_location=True,
                  trim=trim, as_table=as_table, tablefmt=tablefmt)

    def info(self, *messages: Any, extra: Optional[Dict[str, Any]] = None, trim: bool = True):
        self._log("INFO", "ℹ️ INFO", *messages, extra=extra, show_location=True, trim=trim)

    def success(self, *messages: Any, extra: Optional[Dict[str, Any]] = None, trim: bool = True,
                as_table: bool = True, tablefmt: TableFormat = "github"):
        self._log("SUCCESS", "✅ SUCCESS", *messages, extra=extra, show_location=False,
                  trim=trim, as_table=as_table,tablefmt=tablefmt)

    def warning(self, *messages: Any, extra: Optional[Dict[str, Any]] = None, trim: bool = True):
        self._log("WARNING", "⚠️ WARNING", *messages, extra=extra, show_location=True, trim=trim)

    def error(self, *messages: Any, extra: Optional[Dict[str, Any]] = None, trim: bool = False):
        self._log("ERROR", "❌ ERROR", *messages, extra=extra, show_location=True, trim=trim)

    def critical(self, *messages: Any, extra: Optional[Dict[str, Any]] = None, trim: bool = False):
        self._log("CRITICAL", "🔥 CRITICAL", *messages, extra=extra, show_location=True, trim=trim)

    def exception(
        self,
        *messages: Any,
        extra: Optional[Dict[str, Any]] = None,
    ):
        prefix = self._combine_messages(messages) if messages else "An exception occurred"
        tb = traceback.format_exc()
        self._log(
            "ERROR",
            "💥 EXCEPTION",
            prefix,
            "\n" + tb,
            extra=extra,
            show_location=True,
            trim=False,
        )