FROM apache/superset:5.0.0
USER root
# The image ships without database drivers or LDAP support.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libldap2-dev libsasl2-dev \
 && uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.10 python-ldap==3.4.4 \
 && apt-get purge -y build-essential && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /config && chown superset /config
USER superset
