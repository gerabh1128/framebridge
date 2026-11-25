

The private runtime excludes pip, its console launchers and ensurepip. Dependencies are fixed, validated build inputs; clients do not install them with pip. Upstream runtime license notices are retained.

The persistent FBTERM viewer is launched using the distribution Python for GTK/GI/VTE, with only the pure-Python Expanded module path. The worker and engine retain the pinned private interpreter.
