# installation in colab
from git hub
```angular2html
!git clone https://github.com/atherashraf/digitalarzraster.git
%cd digitalarzraster
!pip install -q -r requirements.txt
```

from sync folder
````angular2html
from google.colab import drive
drive.mount('/content/drive')

import sys
sys.path.append('/content/drive/Othercomputers/My Mac/GoogleDriveSync/PyCharmProjects/DigitalArzEngine')
!pip install -q -r "/content/drive/Othercomputers/My Mac/GoogleDriveSync/PyCharmProjects/DigitalArzEngine/digitalarzengine/requirements.txt"

````

authenticating google earth engine

```angular2html 
ee.Authenticate()
# Initialize the library.
ee.Initialize(project='ee-atherashraf-cloud')
```

reloading updated files

```angular2html
import sys
import importlib
import inspect

sys.modules.pop("digitalarzengine.io.rio_raster", None)

# Reload modules
import digitalarzengine
importlib.reload(digitalarzengine)

import digitalarzengine.io.rio_raster as rr
importlib.reload(rr)

```
verify the code changes

````angular2html
# Verify
print(hasattr(rr.RioRaster, "write_to_file"))  # Should be True to check new mehtod exists
print(inspect.getsource(rr.RioRaster.write_to_file))  # Should list get_dataset

````