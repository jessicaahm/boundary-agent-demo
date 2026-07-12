-- Seed data for the demo storefront. Runs once, on first DB init.
-- appuser/apppass and the storefront DB are created by the postgres image from
-- the POSTGRES_* env in docker-compose.yml, so this only needs the schema+rows.

CREATE TABLE IF NOT EXISTS products (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    price NUMERIC(10, 2) NOT NULL
);

INSERT INTO products (name, price) VALUES
    ('Boundary T-shirt', 25.00),
    ('Vault Hoodie',     55.00),
    ('Consul Mug',       12.50),
    ('Terraform Sticker Pack', 5.00)
ON CONFLICT DO NOTHING;

-- appuser owns its schema objects; make sure it can read the table.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO appuser;
