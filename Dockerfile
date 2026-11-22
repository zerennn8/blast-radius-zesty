# Blast Radius submission. Build: fetch the pinned sqlglot source (read-only
# reference for diffing patches). Run: pure-Python selector + committed map.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import urllib.request,tarfile,io,os;\
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read();\
t=tarfile.open(fileobj=io.BytesIO(d));t.extractall('/app',filter='data');\
os.rename('/app/sqlglot-${SQLGLOT_SHA}','/app/sqlglot')" \
 && rm -rf /app/sqlglot/sqlglot-integration-tests /app/sqlglot/benchmarks /app/sqlglot/posts \
 && chmod -R a+rX /app
COPY selector.py map.pkl.z /app/
RUN chmod a+r /app/selector.py /app/map.pkl.z && python -m compileall -q /app/selector.py
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "/app/selector.py"]
