-- Sample sales database shared by both proofs of concept.
-- Three branches, so we can show that each user sees only their own branch.

CREATE TABLE branches (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  city TEXT NOT NULL
);

INSERT INTO branches VALUES
  ('tehran',  'Tehran branch',  'Tehran'),
  ('isfahan', 'Isfahan branch', 'Isfahan'),
  ('shiraz',  'Shiraz branch',  'Shiraz');

CREATE TABLE products (
  id       SERIAL PRIMARY KEY,
  name     TEXT NOT NULL,
  category TEXT NOT NULL,
  price    NUMERIC(12, 2) NOT NULL
);

INSERT INTO products (name, category, price) VALUES
  ('Laptop',      'Electronics', 950),
  ('Phone',       'Electronics', 620),
  ('Headphones',  'Electronics', 85),
  ('Desk',        'Furniture',   240),
  ('Chair',       'Furniture',   130),
  ('Notebook',    'Stationery',  4),
  ('Pen pack',    'Stationery',  6),
  ('Monitor',     'Electronics', 310);

CREATE TABLE sales (
  id         SERIAL PRIMARY KEY,
  sale_date  DATE NOT NULL,
  branch     TEXT NOT NULL REFERENCES branches (code),
  product_id INT  NOT NULL REFERENCES products (id),
  quantity   INT  NOT NULL,
  amount     NUMERIC(12, 2) NOT NULL
);

-- Deterministic pseudo-random data for the last two years: a hash of
-- (day, branch, product) decides whether a sale happened and its quantity.
INSERT INTO sales (sale_date, branch, product_id, quantity, amount)
SELECT day, branch, product_id, 1 + h % 5, (1 + h % 5) * price
FROM (
  SELECT d::date AS day, b.code AS branch, p.id AS product_id, p.price,
         abs(hashtext(d::date::text || b.code || p.id::text)) AS h,
         CASE b.code WHEN 'tehran' THEN 35 WHEN 'isfahan' THEN 22 ELSE 15 END AS chance
  FROM generate_series(CURRENT_DATE - INTERVAL '730 days', CURRENT_DATE, INTERVAL '1 day') AS d
  CROSS JOIN branches b
  CROSS JOIN products p
) candidates
WHERE (h / 7) % 100 < chance;

-- Role used by Metabase and Superset. It can read everything; the BI tool
-- applies the per-user branch filter (locked embed parameter / guest-token RLS).
CREATE ROLE bi_reader LOGIN PASSWORD 'bi_reader';
GRANT USAGE ON SCHEMA public TO bi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO bi_reader;

-- Roles used by the AI service: one login role per branch, plus ai_all for
-- managers. PostgreSQL row-level security decides what each one can read from
-- session_user, which a query can not change (unlike a session setting), so
-- even a malicious generated query can not read another branch's rows.
-- To add a branch: create its role and add a row to security.ai_branch_roles.
CREATE ROLE ai_readers NOLOGIN;
GRANT USAGE ON SCHEMA public TO ai_readers;
GRANT SELECT ON branches, products, sales TO ai_readers;

CREATE SCHEMA security;
CREATE TABLE security.ai_branch_roles (
  role_name NAME PRIMARY KEY,
  branch    TEXT NOT NULL  -- a branch code, or '*' for every branch
);
INSERT INTO security.ai_branch_roles VALUES
  ('ai_tehran', 'tehran'), ('ai_isfahan', 'isfahan'), ('ai_shiraz', 'shiraz'), ('ai_all', '*');

-- Policies run with the querying user's rights, so look the branch up through
-- a SECURITY DEFINER function; the mapping table itself stays private.
CREATE FUNCTION security.ai_branch() RETURNS TEXT
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = security, pg_temp
  AS $$ SELECT branch FROM security.ai_branch_roles WHERE role_name = session_user $$;
REVOKE ALL ON FUNCTION security.ai_branch() FROM PUBLIC;
GRANT USAGE ON SCHEMA security TO ai_readers;
GRANT EXECUTE ON FUNCTION security.ai_branch() TO ai_readers;

DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT role_name FROM security.ai_branch_roles LOOP
    -- Demo password; give every role its own secret in production.
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE ai_readers', r.role_name, 'ai_reader');
    EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', r.role_name, '15s');
    EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', r.role_name);
  END LOOP;
END $$;

ALTER TABLE sales ENABLE ROW LEVEL SECURITY;
CREATE POLICY bi_reader_all ON sales FOR SELECT TO bi_reader USING (true);
CREATE POLICY ai_own_branch ON sales FOR SELECT TO ai_readers
  USING (security.ai_branch() IN ('*', branch));

ALTER TABLE branches ENABLE ROW LEVEL SECURITY;
CREATE POLICY bi_reader_all ON branches FOR SELECT TO bi_reader USING (true);
CREATE POLICY ai_own_branch ON branches FOR SELECT TO ai_readers
  USING (security.ai_branch() IN ('*', code));
