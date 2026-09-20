"""External LeTools provider for the versioned Thor recording contract."""

# LeTools discovers entry points while importing itself. Bootstrap it before
# importing provider submodules so direct Python API imports are also safe.
import letools as _letools

__version__ = "0.1.0"
