# dafastmap/utils/singletons.py
import os
from utils.loggers import DALogger
from settings import APP_NAME, LOG_LEVEL

da_logger = DALogger(
    name=APP_NAME,
    min_level=LOG_LEVEL,
    log_to_file=False,
    log_file_path=f"{APP_NAME.lower()}.log"
)

