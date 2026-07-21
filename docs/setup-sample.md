**How to Build and Upload a Python Package to PyPI**

To publish a Python package, follow these steps:

### 1. Install Required Tools

Install `twine`, a utility for securely uploading Python packages to PyPI:

```bash
pip install twine
```

### 2. Build the Package

Run the following commands to build your package using `setuptools`:

```bash
python setup.py sdist bdist_wheel
```

### 3. Upload the Package to PyPI

Use `twine` to upload the built distribution files:

```bash
twine upload dist/*
```

### 4. Optional: Automate with Shell Script

You can automate the build and upload process with a shell script:

```bash
#!/bin/bash

: '
Create and upload a Python package to PyPI.
Ensure you have updated the version before running this script.
'

set -e  # Exit immediately if a command exits with a non-zero status
set -o pipefail

echo "🔧 Cleaning old builds..."
rm -rf build/ dist/ *.egg-info

echo "📦 Building package (source & wheel)..."
python setup.py sdist bdist_wheel --verbose

echo "🚀 Uploading to PyPI..."
twine upload --skip-existing dist/*

echo "✅ Upload completed successfully."


---

### Sample `setup.py` File

Create a `setup.py` file in the root of your project directory and paste the following code into it:

```python
import setuptools
import os

# Define the path to the directory containing setup.py
lib_folder = os.path.dirname(os.path.realpath(__file__))

# Read the contents of your README file
with open(os.path.join(lib_folder, 'digitalarzengine', 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

# Path to the requirements file
requirement_path = os.path.join(lib_folder, 'digitalarzengine/requirements.txt')
install_requires = []  # List to store requirements

# Read the requirements file and populate the install_requires list
if os.path.isfile(requirement_path):
    with open(requirement_path) as f:
        install_requires = f.read().splitlines()

# Version of the package
VERSION = '0.0.1'
# Short description of the package
DESCRIPTION = 'DigitalArzEngine for GEE, raster and vector data processing'

# Configuration of package setup
setuptools.setup(
    name="digitalarzengine",
    version=VERSION,
    author="Ather Ashraf",
    author_email="atherashraf@gmail.com",
    description=DESCRIPTION,
    long_description=long_description,
    long_description_content_type='text/markdown',
    python_requires='>=3',
    install_requires=install_requires,
    packages=setuptools.find_packages(),
    include_package_data=True,  # Include package data
    package_data={},
    keywords=['raster', 'vector', 'digitalarz'],
    classifiers=[
        "Development Status :: 1 - Planning",
        "Intended Audience :: Education",
        "Programming Language :: Python :: 3",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
    ]
)
```
