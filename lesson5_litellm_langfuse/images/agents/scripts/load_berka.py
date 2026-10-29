"""Load the Berka bank dataset (PKDD'99) into our Postgres.

Berka is a real anonymised dataset of a Czech bank, 1993–1998:
~1M transactions, 4.5k accounts, 5.4k clients, loans, cards, districts.
Amounts are in Czech crowns (CZK).

Source: the public CTU Prague relational repository (MariaDB, guest access).

    1. download each table once into data/berka/*.csv.gz   (cache — rerun is offline)
    2. create an English schema with COMMENTs on every table and column
       (the MCP server shows these comments to the model)
    3. translate the Czech codes (PRIJEM → credit, …) and COPY the rows in

Run:   uv run python scripts/load_berka.py           (skips if already loaded)
       uv run python scripts/load_berka.py --force   (drop and reload)
"""

import csv
import gzip
import os
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()
ADMIN_DSN = os.getenv("ADMIN_DSN", "postgresql://postgres:postgres@localhost:5434/bank")
CACHE = Path(__file__).resolve().parent.parent / "data" / "berka"

SOURCE = dict(host="relational.fel.cvut.cz", port=3306, user="guest",
              password="ctu-relational", database="financial")
SOURCE_TABLES = ["district", "account", "client", "disp", "card", "loan", "order", "trans"]


# ── 1. download (cached) ────────────────────────────────────────────────────

def download() -> None:
    """Stream every source table into a gzipped CSV, once."""
    missing = [t for t in SOURCE_TABLES if not (CACHE / f"{t}.csv.gz").exists()]
    if not missing:
        print("✓ dataset already cached in", CACHE)
        return
    import pymysql
    import pymysql.cursors

    CACHE.mkdir(parents=True, exist_ok=True)
    conn = pymysql.connect(**SOURCE, connect_timeout=20, cursorclass=pymysql.cursors.SSCursor)
    for table in missing:
        started, rows = time.time(), 0
        tmp = CACHE / f"{table}.csv.gz.part"
        with conn.cursor() as cur, gzip.open(tmp, "wt", newline="") as f:
            cur.execute(f"SELECT * FROM `{table}`")
            writer = csv.writer(f)
            writer.writerow([d[0] for d in cur.description])
            for row in cur:
                writer.writerow(["" if v is None else v for v in row])
                rows += 1
        tmp.rename(CACHE / f"{table}.csv.gz")
        print(f"  ↓ {table:<8} {rows:>9,} rows  ({time.time() - started:.1f}s)")
    conn.close()


def read_rows(table: str):
    """Yield dicts from the cached CSV; empty strings become None."""
    with gzip.open(CACHE / f"{table}.csv.gz", "rt", newline="") as f:
        for row in csv.DictReader(f):
            yield {k: (v if v != "" else None) for k, v in row.items()}


# ── 2. schema ───────────────────────────────────────────────────────────────

SCHEMA = """
DROP TABLE IF EXISTS transactions, orders, loans, cards, dispositions, clients, accounts, districts CASCADE;

CREATE TABLE districts (
    district_id        int PRIMARY KEY,
    name               text,
    region             text,
    inhabitants        int,
    cities             int,
    urban_ratio        numeric(4,1),
    avg_salary         int,
    unemployment_95    numeric(4,2),
    unemployment_96    numeric(4,2),
    entrepreneurs_per_1000 int,
    crimes_95          int,
    crimes_96          int
);
COMMENT ON TABLE districts IS 'Czech districts where clients live and accounts are opened (demographics).';
COMMENT ON COLUMN districts.name IS 'District name, e.g. Hl.m. Praha (= Prague).';
COMMENT ON COLUMN districts.region IS 'Region the district belongs to, e.g. Prague, south Moravia.';
COMMENT ON COLUMN districts.urban_ratio IS 'Share of urban inhabitants, %.';
COMMENT ON COLUMN districts.avg_salary IS 'Average monthly salary in the district, CZK.';
COMMENT ON COLUMN districts.unemployment_95 IS 'Unemployment rate in 1995, %. NULL = unknown.';
COMMENT ON COLUMN districts.unemployment_96 IS 'Unemployment rate in 1996, %.';
COMMENT ON COLUMN districts.crimes_95 IS 'Number of committed crimes in 1995. NULL = unknown.';

CREATE TABLE accounts (
    account_id          int PRIMARY KEY,
    district_id         int REFERENCES districts,
    statement_frequency text,
    opened_on           date
);
COMMENT ON TABLE accounts IS 'Bank accounts. Balance is NOT stored here — take the balance of the latest transaction.';
COMMENT ON COLUMN accounts.district_id IS 'District of the bank branch where the account was opened.';
COMMENT ON COLUMN accounts.statement_frequency IS 'How often statements are issued: monthly | weekly | after_transaction.';

CREATE TABLE clients (
    client_id   int PRIMARY KEY,
    gender      char(1),
    birth_date  date,
    district_id int REFERENCES districts
);
COMMENT ON TABLE clients IS 'Bank clients (people). Linked to accounts through dispositions.';
COMMENT ON COLUMN clients.gender IS 'M or F.';

CREATE TABLE dispositions (
    disp_id    int PRIMARY KEY,
    client_id  int REFERENCES clients,
    account_id int REFERENCES accounts,
    role       text
);
COMMENT ON TABLE dispositions IS 'Who may use which account (client ↔ account link).';
COMMENT ON COLUMN dispositions.role IS 'owner — the account owner (exactly one per account); user — a person allowed to use it.';

CREATE TABLE cards (
    card_id   int PRIMARY KEY,
    disp_id   int REFERENCES dispositions,
    card_type text,
    issued_on date
);
COMMENT ON TABLE cards IS 'Payment cards issued to an account disposition.';
COMMENT ON COLUMN cards.card_type IS 'junior | classic | gold.';

CREATE TABLE loans (
    loan_id         int PRIMARY KEY,
    account_id      int REFERENCES accounts,
    granted_on      date,
    amount          numeric(12,2),
    duration_months int,
    monthly_payment numeric(12,2),
    status          text
);
COMMENT ON TABLE loans IS 'Loans granted to accounts. Amounts in CZK.';
COMMENT ON COLUMN loans.status IS 'finished_ok | finished_default (not paid back) | running_ok | running_in_debt.';

CREATE TABLE orders (
    order_id   int PRIMARY KEY,
    account_id int REFERENCES accounts,
    bank_to    text,
    account_to bigint,
    amount     numeric(12,2),
    purpose    text
);
COMMENT ON TABLE orders IS 'Standing payment orders (regular automatic payments from an account). CZK.';
COMMENT ON COLUMN orders.purpose IS 'insurance | household (rent, utilities) | leasing | loan_payment; NULL = other.';

CREATE TABLE transactions (
    trans_id        int PRIMARY KEY,
    account_id      int REFERENCES accounts,
    trans_date      date,
    direction       text,
    operation       text,
    amount          numeric(12,2),
    balance         numeric(12,2),
    category        text,
    partner_bank    text,
    partner_account bigint
);
COMMENT ON TABLE transactions IS 'All account transactions 1993–1998 (~1M rows). Amounts are positive; direction says in/out. CZK.';
COMMENT ON COLUMN transactions.direction IS 'credit (money in) | debit (money out).';
COMMENT ON COLUMN transactions.operation IS 'card_withdrawal | cash_deposit | cash_withdrawal | transfer_in (from another bank) | transfer_out (to another bank) | interest_credit.';
COMMENT ON COLUMN transactions.amount IS 'Transaction amount, CZK, always positive.';
COMMENT ON COLUMN transactions.balance IS 'Account balance right AFTER this transaction, CZK.';
COMMENT ON COLUMN transactions.category IS 'insurance | statement_fee | interest | penalty_interest (negative balance) | household | pension | loan_payment; NULL = not specified.';
COMMENT ON COLUMN transactions.partner_bank IS 'Two-letter code of the partner bank for transfers.';

COMMENT ON COLUMN districts.district_id IS 'District id.';
COMMENT ON COLUMN districts.inhabitants IS 'Number of inhabitants.';
COMMENT ON COLUMN districts.cities IS 'Number of cities in the district.';
COMMENT ON COLUMN districts.entrepreneurs_per_1000 IS 'Entrepreneurs per 1000 inhabitants.';
COMMENT ON COLUMN districts.crimes_96 IS 'Number of committed crimes in 1996.';
COMMENT ON COLUMN accounts.account_id IS 'Account id.';
COMMENT ON COLUMN accounts.opened_on IS 'Date the account was opened.';
COMMENT ON COLUMN clients.client_id IS 'Client id.';
COMMENT ON COLUMN clients.birth_date IS 'Date of birth.';
COMMENT ON COLUMN clients.district_id IS 'Where the client lives → districts.';
COMMENT ON COLUMN dispositions.disp_id IS 'Disposition id.';
COMMENT ON COLUMN dispositions.client_id IS '→ clients.';
COMMENT ON COLUMN dispositions.account_id IS '→ accounts.';
COMMENT ON COLUMN cards.card_id IS 'Card id.';
COMMENT ON COLUMN cards.disp_id IS 'Card holder → dispositions (join dispositions to get the account and client).';
COMMENT ON COLUMN cards.issued_on IS 'Date the card was issued.';
COMMENT ON COLUMN loans.loan_id IS 'Loan id.';
COMMENT ON COLUMN loans.account_id IS '→ accounts.';
COMMENT ON COLUMN loans.granted_on IS 'Date the loan was granted.';
COMMENT ON COLUMN loans.amount IS 'Loan amount, CZK.';
COMMENT ON COLUMN loans.duration_months IS 'Loan duration in months.';
COMMENT ON COLUMN loans.monthly_payment IS 'Monthly installment, CZK.';
COMMENT ON COLUMN orders.order_id IS 'Order id.';
COMMENT ON COLUMN orders.account_id IS 'Payer → accounts.';
COMMENT ON COLUMN orders.bank_to IS 'Two-letter code of the recipient bank.';
COMMENT ON COLUMN orders.account_to IS 'Recipient account number.';
COMMENT ON COLUMN orders.amount IS 'Payment amount, CZK.';
COMMENT ON COLUMN transactions.trans_id IS 'Transaction id.';
COMMENT ON COLUMN transactions.account_id IS '→ accounts.';
COMMENT ON COLUMN transactions.trans_date IS 'Transaction date.';
COMMENT ON COLUMN transactions.partner_account IS 'Partner account number for transfers.';
"""

INDEXES = """
CREATE INDEX ON transactions (account_id, trans_date);
CREATE INDEX ON transactions (trans_date);
CREATE INDEX ON dispositions (account_id);
CREATE INDEX ON loans (account_id);
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;
ANALYZE;
"""

# ── 3. Czech codes → English ────────────────────────────────────────────────

FREQUENCY = {"POPLATEK MESICNE": "monthly", "POPLATEK TYDNE": "weekly", "POPLATEK PO OBRATU": "after_transaction"}
ROLE = {"OWNER": "owner", "DISPONENT": "user"}
LOAN_STATUS = {"A": "finished_ok", "B": "finished_default", "C": "running_ok", "D": "running_in_debt"}
PURPOSE = {"POJISTNE": "insurance", "SIPO": "household", "LEASING": "leasing", "UVER": "loan_payment"}
DIRECTION = {"PRIJEM": "credit", "VYDAJ": "debit", "VYBER": "debit"}
OPERATION = {"VYBER KARTOU": "card_withdrawal", "VKLAD": "cash_deposit", "VYBER": "cash_withdrawal",
             "PREVOD Z UCTU": "transfer_in", "PREVOD NA UCET": "transfer_out"}
CATEGORY = {"POJISTNE": "insurance", "SLUZBY": "statement_fee", "UROK": "interest",
            "SANKC. UROK": "penalty_interest", "SIPO": "household", "DUCHOD": "pension",
            "UVER": "loan_payment"}


def code(mapping: dict, value):
    """Translate a code; blanks become NULL."""
    if value is None or not value.strip():
        return None
    return mapping[value]


TABLES = [  # (target table, source table, row transform)
    ("districts", "district", lambda r: (
        r["district_id"], r["A2"], r["A3"], r["A4"], r["A9"], r["A10"], r["A11"],
        r["A12"], r["A13"], r["A14"], r["A15"], r["A16"])),
    ("accounts", "account", lambda r: (
        r["account_id"], r["district_id"], code(FREQUENCY, r["frequency"]), r["date"])),
    ("clients", "client", lambda r: (
        r["client_id"], r["gender"], r["birth_date"], r["district_id"])),
    ("dispositions", "disp", lambda r: (
        r["disp_id"], r["client_id"], r["account_id"], code(ROLE, r["type"]))),
    ("cards", "card", lambda r: (
        r["card_id"], r["disp_id"], r["type"], r["issued"])),
    ("loans", "loan", lambda r: (
        r["loan_id"], r["account_id"], r["date"], r["amount"], r["duration"],
        r["payments"], code(LOAN_STATUS, r["status"]))),
    ("orders", "order", lambda r: (
        r["order_id"], r["account_id"], r["bank_to"], r["account_to"], r["amount"],
        code(PURPOSE, r["k_symbol"]))),
    ("transactions", "trans", lambda r: (
        r["trans_id"], r["account_id"], r["date"], DIRECTION[r["type"]],
        # every row without an operation is the bank crediting interest
        OPERATION[r["operation"]] if r["operation"] else "interest_credit",
        r["amount"], r["balance"], code(CATEGORY, r["k_symbol"]), r["bank"], r["account"])),
]


def already_loaded(conn) -> bool:
    exists = conn.execute("SELECT to_regclass('public.transactions')").fetchone()[0]
    return bool(exists) and conn.execute("SELECT count(*) FROM transactions").fetchone()[0] > 0


def load(force: bool) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        if already_loaded(conn) and not force:
            print("✓ bank database already loaded (use --force to reload)")
            return
        conn.execute(SCHEMA)
        for target, source, transform in TABLES:
            started, rows = time.time(), 0
            with conn.cursor() as cur, cur.copy(f"COPY {target} FROM STDIN") as copy:
                for row in read_rows(source):
                    copy.write_row(transform(row))
                    rows += 1
            print(f"  → {target:<13} {rows:>9,} rows  ({time.time() - started:.1f}s)")
        conn.execute(INDEXES)
    print("✓ bank database ready")


if __name__ == "__main__":
    download()
    load(force="--force" in sys.argv)
