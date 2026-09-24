"""支持 python -m tokenledger 直接调用。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())