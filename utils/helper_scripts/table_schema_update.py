from sqlalchemy import inspect, text



class TableSchemaUpdater:
    def __init__(self, engine, logger=None):
        self.engine = engine
        self.logger = logger
        self.dialect = engine.dialect.name

    def log(self, msg, level="info"):
        if self.logger:
            fn = getattr(self.logger, level, None)
            if callable(fn):
                fn(msg)
                return
        print(msg)

    def refresh_inspector(self):
        self.inspector = inspect(self.engine)

    def get_table_names(self):
        self.refresh_inspector()
        return self.inspector.get_table_names()

    def get_columns_info(self, table_name):
        self.refresh_inspector()
        return {c["name"]: c for c in self.inspector.get_columns(table_name)}

    def column_type_sql(self, column):
        return column.type.compile(dialect=self.engine.dialect)

    def server_default_sql(self, column):
        if not column.server_default:
            return ""

        arg = column.server_default.arg
        if hasattr(arg, "text"):
            return f"DEFAULT {arg.text}"
        return f"DEFAULT {str(arg)}"

    def column_def_sql(self, column, include_name=False):
        parts = []

        if include_name:
            parts.append(f'"{column.name}"')

        parts.append(self.column_type_sql(column))
        parts.append("NULL" if column.nullable else "NOT NULL")

        default_sql = self.server_default_sql(column)
        if default_sql:
            parts.append(default_sql)

        return " ".join(parts)

    def create_table(self, table_obj):
        table_obj.create(self.engine)
        self.log(f"📦 Created new table: {table_obj.name}")

    def add_column(self, table_name, column):
        if self.dialect == "sqlite" and hasattr(column.type, "item_type"):
            self.log(f"⚠️ Skipping Array column {table_name}.{column.name} - Not supported in SQLite")
            return

        col_def = self.column_def_sql(column, include_name=False)
        sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_def}'
        self.log(sql, level="debug")

        with self.engine.begin() as conn:
            conn.execute(text(sql))

        self.log(f"✨ Added column [{column.name}] (Type: {type(column.type).__name__}) to {table_name}")

    def rename_column(self, table_name, old_name, new_name):
        if self.dialect in ("postgresql", "sqlite"):
            sql = f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_name}" TO "{new_name}"'
        else:
            raise NotImplementedError(
                f"Column rename not implemented for dialect: {self.dialect}"
            )

        with self.engine.begin() as conn:
            conn.execute(text(sql))

        self.log(f"🔁 Renamed column {table_name}.{old_name} -> {new_name}")

    def alter_nullable(self, table_name, column):
        if self.dialect == "postgresql":
            if column.nullable:
                sql = f'ALTER TABLE "{table_name}" ALTER COLUMN "{column.name}" DROP NOT NULL'
            else:
                sql = f'ALTER TABLE "{table_name}" ALTER COLUMN "{column.name}" SET NOT NULL'

        elif self.dialect in ("mysql", "mariadb"):
            null_stmt = "NULL" if column.nullable else "NOT NULL"
            default_sql = self.server_default_sql(column)
            sql = (
                f'ALTER TABLE "{table_name}" MODIFY COLUMN "{column.name}" '
                f'{self.column_type_sql(column)} {null_stmt} {default_sql}'
            ).strip()

        elif self.dialect == "sqlite":
            self.log(
                f"⚠️ SQLite cannot alter nullable directly for {table_name}.{column.name}; skipping."
            )
            return

        else:
            raise NotImplementedError(
                f"Nullable alter not implemented for dialect: {self.dialect}"
            )

        with self.engine.begin() as conn:
            conn.execute(text(sql))

        self.log(f"🛠 Updated nullable for {table_name}.{column.name} -> {column.nullable}")

    def sync_table(self, table_obj, rename_map=None):
        """
        rename_map format:
        {
            "old_col_name": "new_col_name"
        }
        """
        rename_map = rename_map or {}
        table_name = table_obj.name

        self.log(f"Syncing table '{table_name}'")

        if table_name not in self.get_table_names():
            self.create_table(table_obj)
            return

        existing_cols = self.get_columns_info(table_name)

        # 1) Rename columns
        for old_name, new_name in rename_map.items():
            if old_name in existing_cols and new_name not in existing_cols:
                try:
                    self.rename_column(table_name, old_name, new_name)
                except Exception as e:
                    self.log(
                        f"❌ Failed renaming {table_name}.{old_name} -> {new_name}: {e}",
                        level="error",
                    )

        existing_cols = self.get_columns_info(table_name)

        # 2) Add missing columns
        for column in table_obj.columns:
            if column.name not in existing_cols:
                try:
                    self.add_column(table_name, column)
                except Exception as e:
                    self.log(
                        f"❌ Failed adding column {table_name}.{column.name}: {e}",
                        level="error",
                    )

        existing_cols = self.get_columns_info(table_name)

        # 3) Update nullable
        for column in table_obj.columns:
            if column.name not in existing_cols:
                continue

            db_nullable = existing_cols[column.name].get("nullable", True)
            model_nullable = column.nullable

            if db_nullable != model_nullable:
                try:
                    self.alter_nullable(table_name, column)
                except Exception as e:
                    self.log(
                        f"❌ Failed updating nullable for {table_name}.{column.name}: {e}",
                        level="error",
                    )