import glob
import os
import shutil
from pathlib import Path
from typing import Union

try:
    import resource  # Unix-like systems
except Exception:
    resource = None
    try:
        import msvcrt  # Windows-only
    except Exception:
        msvcrt = None



class FileIO:
    @classmethod
    def mkdirs_list(cls, file_paths: list):
        for fp in file_paths:
            cls.mkdirs(fp)


    @staticmethod
    def mkdirs(file_path: Union[str, Path]):
        # Convert to Path object if it's a string
        path_obj = Path(file_path)

        # If the path has a suffix (e.g., .tif, .gpkg), get the parent directory
        # Otherwise, assume the path itself is a directory
        if path_obj.suffix:
            dir_name = path_obj.parent
        else:
            dir_name = path_obj

        # Create the directory if it doesn't exist
        if not dir_name.exists():
            dir_name.mkdir(parents=True, exist_ok=True)

        return dir_name

    @classmethod
    def mvFile(cls, source_path, destination_folder):
        shutil.move(source_path, destination_folder)

    @staticmethod
    def get_file_count(img_folder, ext="tif", include_sub_folder=False):
        """
        @param img_folder:
        @param ext: like tif, xlsx, or *  (to include all file pass *)
        @param include_sub_folder: if you want to count file in sub folder tooo
        @return:
        """
        return len(glob.glob(os.path.join(img_folder, f'*.{ext}'), recursive=include_sub_folder))

    @staticmethod
    def get_file_reading_limit():
        """
        Soft Limit: Adjusting this allows applications to temporarily change their resource usage without impacting the entire system or requiring administrative intervention.
        Hard Limit: This acts as a safeguard to ensure that no single process can exhaust system resources beyond a certain threshold, potentially affecting the stability of the system.
        @return:
        """
        # Use POSIX resource limits when available; otherwise provide a high sentinel on Windows.
        if resource is not None:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            return soft, hard
        # On Windows, resource is unavailable; return large sentinel limits to skip adjustments.
        # If you still hit OS-level limits when merging thousands of files, consider chunking the merge.
        return (10 ** 9, 10 ** 9)

    @classmethod
    def set_file_reading_limit(cls, new_soft_limit):
        # Function to set a new soft limit (and optionally the hard limit)
        # No-op on Windows where 'resource' is unavailable.
        if resource is None:
            print("Skipping file descriptor limit adjustment: not supported on this platform.")
            return

        soft, hard = cls.get_file_reading_limit()
        if new_soft_limit < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft_limit, hard))
            print("Soft limit set to", new_soft_limit)
        else:
            print(f"cannot set soft limit {new_soft_limit} more than hard limit {hard}")

    @staticmethod
    def get_file_name_ext(fp):
        base_name = os.path.basename(fp)
        sfp = base_name.split(".")
        return ",".join(sfp[:-1]), sfp[-1]
