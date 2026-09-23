from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    return create_engine(url or config.DATABASE_URL, future=True)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(eng=None) -> None:
    """Create tables. On PostgreSQL also install the no-double-booking exclusion constraint."""
    from . import models  # noqa: F401  (register tables)

    eng = eng or engine
    if eng.dialect.name == "postgresql":
        with eng.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    Base.metadata.create_all(eng)
    if eng.dialect.name == "postgresql":
        with eng.begin() as conn:
            conn.execute(text("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'assignment_no_overlap') THEN
                    ALTER TABLE assignment ADD CONSTRAINT assignment_no_overlap
                      EXCLUDE USING gist (version_id WITH =, nurse_id WITH =, during WITH &&);
                  END IF;
                END $$;
            """))
            # Published versions are immutable: block edits to their assignments.
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION forbid_published_edit() RETURNS trigger AS $$
                DECLARE st text;
                BEGIN
                  SELECT status INTO st FROM schedule_version
                    WHERE id = COALESCE(NEW.version_id, OLD.version_id);
                  IF st = 'published' OR st = 'superseded' THEN
                    RAISE EXCEPTION 'schedule version % is published and immutable',
                      COALESCE(NEW.version_id, OLD.version_id);
                  END IF;
                  RETURN COALESCE(NEW, OLD);
                END $$ LANGUAGE plpgsql;
                DROP TRIGGER IF EXISTS assignment_immutable ON assignment;
                CREATE TRIGGER assignment_immutable BEFORE INSERT OR UPDATE OR DELETE ON assignment
                  FOR EACH ROW EXECUTE FUNCTION forbid_published_edit();
            """))
            # Backstop for the publish gate: a version can only become 'published' if a
            # valid kernel certificate exists for exactly this instance + schedule.
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION require_certificate() RETURNS trigger AS $$
                BEGIN
                  IF NEW.status = 'published' AND OLD.status IS DISTINCT FROM 'published' THEN
                    IF NOT EXISTS (
                      SELECT 1 FROM verification v
                       WHERE v.version_id = NEW.id AND v.mode = 'kernel' AND v.valid
                         AND v.instance_hash = NEW.instance_hash
                         AND v.schedule_hash = NEW.schedule_hash) THEN
                      RAISE EXCEPTION 'cannot publish version %: no valid Lean certificate', NEW.id;
                    END IF;
                  END IF;
                  RETURN NEW;
                END $$ LANGUAGE plpgsql;
                DROP TRIGGER IF EXISTS version_publish_gate ON schedule_version;
                CREATE TRIGGER version_publish_gate BEFORE UPDATE ON schedule_version
                  FOR EACH ROW EXECUTE FUNCTION require_certificate();
            """))


def drop_all(eng=None) -> None:
    from . import models  # noqa: F401

    Base.metadata.drop_all(eng or engine)
