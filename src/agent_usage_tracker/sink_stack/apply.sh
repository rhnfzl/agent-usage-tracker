#!/bin/sh
# Create the two login roles and apply the sink schema. Every file is idempotent, so this runs on
# every `docker compose up`: a newer aut release reaches an existing database, not only a new one.
# Needs psql 15 or newer and the libpq environment of a superuser (PGHOST, PGPORT, PGUSER,
# PGPASSWORD, PGDATABASE), plus AUT_TZ. AUT_WRITER_PASSWORD and AUT_RO_PASSWORD set the two role
# passwords; when one is unset, that role's existing password is left alone.
set -eu
here=$(dirname "$0")
: "${AUT_TZ:?set AUT_TZ to an IANA time zone, for example Europe/Berlin}"
case "$AUT_TZ" in
  *[!A-Za-z0-9_+/-]*) echo "apply: AUT_TZ has an unexpected character: $AUT_TZ" >&2; exit 2 ;;
esac
psql="psql -X -q -v ON_ERROR_STOP=1"
known=$($psql -tAc "SELECT count(*) FROM pg_timezone_names WHERE name = '$AUT_TZ'")
if [ "$known" != 1 ]; then
  echo "apply: Postgres does not know the time zone $AUT_TZ" >&2
  exit 2
fi
# Rows already written keep the days of the zone they were counted in, so a change mixes days.
stored=$($psql -tAc "SELECT coalesce(current_setting('aut.ledger_tz', true), '')")
if [ -n "$stored" ] && [ "$stored" != "$AUT_TZ" ] && [ "${AUT_TZ_FORCE:-}" != 1 ]; then
  echo "apply: this sink counts days in $stored, not $AUT_TZ. Set AUT_TZ=$stored," \
       "or AUT_TZ_FORCE=1 to switch and accept that older rows keep their old days" >&2
  exit 2
fi

# A newer image ships a newer extension, but a database keeps the version it loaded until this.
# It must be the first statement of its session.
if [ "$($psql -tAc "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")" = 1 ]; then
  $psql -c "ALTER EXTENSION timescaledb UPDATE"
fi

# Roles first: the grants file skips a role that does not exist, without an error.
# \getenv keeps the passwords off the command line, where other users can read them.
$psql <<'SQL'
\getenv writer_pw AUT_WRITER_PASSWORD
\getenv ro_pw AUT_RO_PASSWORD
\getenv tz AUT_TZ
SELECT format('CREATE ROLE %I LOGIN', r) FROM unnest(ARRAY['agents_writer', 'agents_ro']) AS r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) \gexec
\if :{?writer_pw}
ALTER ROLE agents_writer PASSWORD :'writer_pw';
\endif
\if :{?ro_pw}
ALTER ROLE agents_ro PASSWORD :'ro_pw';
\endif
-- Collectors compare their ledger_timezone with this. Grafana's raw-table panels read hours and
-- months in the session zone, so agents_ro sessions use the sink's zone too.
SELECT format('ALTER DATABASE %I SET aut.ledger_tz = %L', current_database(), :'tz') \gexec
SELECT format('ALTER ROLE agents_ro IN DATABASE %I SET timezone = %L', current_database(), :'tz') \gexec
SQL

# Each file in its own psql run and not in one transaction: a continuous aggregate cannot be
# created inside a transaction block. The order matters: the cost views read the price table,
# and the grants name objects from every earlier file.
for f in 01_schema 02_price 03_effort 04_grants; do
  if [ ! -r "$here/sql/$f.sql" ]; then
    echo "apply: cannot read $here/sql/$f.sql" >&2
    exit 2
  fi
  sed "s#@AUT_TZ@#$AUT_TZ#g" "$here/sql/$f.sql" | $psql -f - >/dev/null
done
echo "apply: aut sink schema is current (time zone $AUT_TZ)"
