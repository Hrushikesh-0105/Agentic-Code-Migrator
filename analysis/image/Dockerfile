# Layer 0 static-analysis toolchain. Code under analysis is mounted read-only at /code, results are written to
# /out, and nothing is ever executed from /code. Python 3.12: the newest version every pinned tool supports.
# Rejected (see analysis/README.md): PyCG (archived, breaks on 3.12+), Scalpel (needs typed-ast, won't build),
# python-graphs (CFG/data-flow broken on 3.12), pytype (sunset at 3.12).
FROM python:3.12-slim
RUN pip install --no-cache-dir --root-user-action=ignore \
      "libcst==1.9.0" \
      "tree-sitter==0.26.0" "tree-sitter-python==0.25.0" "tree-sitter-java==0.23.5" \
      "fissix==24.4.24" "modernize==0.8.0" \
      "pyan3==2.8.1" "jarviscg==0.1.0rc7" "grimp==3.17" "networkx==3.7" \
      "pyright[nodejs]==1.1.414" "staticfg==0.9.5" \
      "lizard==1.24.0" "radon==6.0.1" "stdlib-list==0.12.0" \
      "tree-sitter-language-pack==1.21.0" \
 && useradd -m -u 1000 sandbox
USER sandbox
# Warm first-run caches in ~/.cache now, since analysis runs with --network none and a read-only filesystem:
# pyright fetches its npm package, fissix (also used by modernize) pickles its grammars
# tree-sitter-language-pack downloads grammars on first use: fetch every source language's grammar now (Layer 0
# decomposes non-Python sources with them; analysis runs offline)
RUN python -c "import tree_sitter_language_pack as t; L = ['python', 'java', 'javascript', 'c', 'cpp', 'csharp', 'go', 'rust', 'matlab']; t.download(L); [t.get_parser(x) for x in L]"
RUN pyright --version \
 && python -c "from fissix import refactor as r; r.RefactoringTool(r.get_fixers_from_package('fissix.fixes')).refactor_string('print 1\n', 'w')"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
WORKDIR /work
