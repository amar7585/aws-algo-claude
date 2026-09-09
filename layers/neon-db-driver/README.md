# pg8000-driver layer

A Lambda layer carrying [pg8000](https://github.com/tlocke/pg8000), the
PostgreSQL driver every function in this repo uses to reach Neon.

## Why pg8000 and not psycopg2

pg8000 is **pure Python**. There is no libpq and no compiled extension module,
so:

- the layer builds with a plain `pip install` on any machine — no Docker, no
  `manylinux` wheel wrangling, no Amazon Linux build container;
- it is architecture-independent, so the same zip works on `x86_64` and
  `arm64`;
- it stays small, which keeps the functions inside Lambda's zip limits.

`psycopg2` needs a compiled binary matched to the Lambda runtime, and
`psycopg2-binary` is explicitly not recommended for production. Avoiding that
whole class of packaging problem is the point of this layer.

pg8000 pulls in `scramp` and `asn1crypto` transitively. Both are pure Python
and come along with the same `pip install`.

## Building the layer

Lambda expects a Python layer's packages under `python/` at the root of the
zip. From this directory:

```bash
rm -rf build python-layer.zip && mkdir -p build/python
pip install -r requirements.txt --target build/python
cd build && zip -r ../python-layer.zip python && cd ..
```

Publishing it:

```bash
aws lambda publish-layer-version \
  --layer-name pg8000-driver \
  --description "Pure-Python PostgreSQL driver for Neon" \
  --zip-file fileb://python-layer.zip \
  --compatible-runtimes python3.12 \
  --compatible-architectures x86_64 arm64
```

Then attach the returned `LayerVersionArn` to each function that needs it.
Build outputs (`build/`, `python-layer.zip`) are artifacts — do not commit them.

## Version pinning

`requirements.txt` here is the source of truth for the pg8000 version. Keep it
identical to `instrument-master-loader/requirements.txt`, which lists pg8000
only so the handler can be imported and tested locally — the function package
itself must never bundle it, or the copy in the package will shadow the layer.

Publishing a new layer version does not update any function; the function's
layer ARN is version-pinned and has to be repointed explicitly.

## Connecting to Neon

Neon requires TLS. pg8000 takes discrete keyword arguments rather than a
`postgres://` DSN, so connection strings are parsed and passed through with an
explicit SSL context — see `connect()` in
[`instrument-master-loader/handler.py`](../../instrument-master-loader/handler.py).

pg8000's DB-API layer (`pg8000.dbapi`) uses `format` paramstyle, so queries are
written with `%s` placeholders. Note there is no `execute_values` helper as in
psycopg2 — multi-row upserts build their own `VALUES` tuples in batches.
