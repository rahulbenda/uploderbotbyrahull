import sqlite3
from pathlib import Path
from datetime import datetime


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

DATABASE_FILE = Path("uploader.db")


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():

    connection = sqlite3.connect(
        DATABASE_FILE
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# INITIALIZE DATABASE
# ============================================================

def init_database():

    connection = get_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # Create main history table
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id INTEGER,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            file_name TEXT,
            file_type TEXT,
            file_size INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )

    # --------------------------------------------------------
    # Database migration for older database
    # --------------------------------------------------------

    cursor.execute(
        "PRAGMA table_info(upload_history)"
    )

    existing_columns = {
        row["name"]
        for row in cursor.fetchall()
    }

    if "file_type" not in existing_columns:

        cursor.execute(
            """
            ALTER TABLE upload_history
            ADD COLUMN file_type TEXT
            """
        )

    if "file_size" not in existing_columns:

        cursor.execute(
            """
            ALTER TABLE upload_history
            ADD COLUMN file_size INTEGER
            """
        )

    connection.commit()

    connection.close()


# ============================================================
# ADD HISTORY
# ============================================================

def add_history(
    queue_id: int,
    user_id: int,
    url: str,
    file_name: str = None,
    file_type: str = None,
    file_size: int = None,
    status: str = "waiting",
    error: str = None,
):

    connection = get_connection()

    cursor = connection.cursor()

    created_at = datetime.now().isoformat(
        timespec="seconds"
    )

    cursor.execute(
        """
        INSERT INTO upload_history
        (
            queue_id,
            user_id,
            url,
            file_name,
            file_type,
            file_size,
            status,
            error,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            queue_id,
            user_id,
            url,
            file_name,
            file_type,
            file_size,
            status,
            error,
            created_at,
        ),
    )

    connection.commit()

    history_id = cursor.lastrowid

    connection.close()

    return history_id


# ============================================================
# UPDATE HISTORY
# ============================================================

def update_history(
    history_id: int,
    status: str,
    file_name: str = None,
    file_type: str = None,
    file_size: int = None,
    error: str = None,
):

    connection = get_connection()

    cursor = connection.cursor()

    completed_at = None

    if status in (
        "completed",
        "failed",
        "cancelled",
    ):

        completed_at = datetime.now().isoformat(
            timespec="seconds"
        )

    cursor.execute(
        """
        UPDATE upload_history
        SET
            status = ?,
            file_name = COALESCE(?, file_name),
            file_type = COALESCE(?, file_type),
            file_size = COALESCE(?, file_size),
            error = ?,
            completed_at = COALESCE(?, completed_at)
        WHERE id = ?
        """,
        (
            status,
            file_name,
            file_type,
            file_size,
            error,
            completed_at,
            history_id,
        ),
    )

    connection.commit()

    connection.close()


# ============================================================
# GET HISTORY
# ============================================================

def get_history(
    user_id: int,
    limit: int = 20,
):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM upload_history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            user_id,
            limit,
        ),
    )

    rows = cursor.fetchall()

    connection.close()

    return rows


# ============================================================
# GET SINGLE HISTORY RECORD
# ============================================================

def get_history_item(
    history_id: int,
    user_id: int,
):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM upload_history
        WHERE id = ?
        AND user_id = ?
        """,
        (
            history_id,
            user_id,
        ),
    )

    row = cursor.fetchone()

    connection.close()

    return row


# ============================================================
# CHECK DUPLICATE URL
# ============================================================

def find_duplicate_url(
    user_id: int,
    url: str,
):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM upload_history
        WHERE user_id = ?
        AND url = ?
        AND status = 'completed'
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            user_id,
            url,
        ),
    )

    row = cursor.fetchone()

    connection.close()

    return row


# ============================================================
# CHECK DUPLICATE FILE
# ============================================================

def find_duplicate_file(
    user_id: int,
    file_name: str,
    file_size: int,
):

    if not file_name or not file_size:

        return None

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM upload_history
        WHERE user_id = ?
        AND file_name = ?
        AND file_size = ?
        AND status = 'completed'
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            user_id,
            file_name,
            file_size,
        ),
    )

    row = cursor.fetchone()

    connection.close()

    return row


# ============================================================
# STATISTICS
# ============================================================

def get_statistics(
    user_id: int,
):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            COUNT(*) AS total,

            SUM(
                CASE
                    WHEN status = 'completed'
                    THEN 1
                    ELSE 0
                END
            ) AS completed,

            SUM(
                CASE
                    WHEN status = 'failed'
                    THEN 1
                    ELSE 0
                END
            ) AS failed,

            SUM(
                CASE
                    WHEN status = 'cancelled'
                    THEN 1
                    ELSE 0
                END
            ) AS cancelled,

            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'completed'
                        THEN COALESCE(file_size, 0)
                        ELSE 0
                    END
                ),
                0
            ) AS total_bytes

        FROM upload_history
        WHERE user_id = ?
        """,
        (
            user_id,
        ),
    )

    row = cursor.fetchone()

    connection.close()

    return {
        "total": row["total"] or 0,
        "completed": row["completed"] or 0,
        "failed": row["failed"] or 0,
        "cancelled": row["cancelled"] or 0,
        "total_bytes": row["total_bytes"] or 0,
    }


# ============================================================
# CLEAR HISTORY
# ============================================================

def clear_history(
    user_id: int,
):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        DELETE FROM upload_history
        WHERE user_id = ?
        """,
        (
            user_id,
        ),
    )

    deleted = cursor.rowcount

    connection.commit()

    connection.close()

    return deleted


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    init_database()

    print(
        "💾 SQLite Database initialized successfully."
    )

    print(
        f"📁 Database file: {DATABASE_FILE}"
    )

    print(
        "🧠 Smart file metadata enabled."
    )

    print(
        "🔁 Duplicate detection enabled."
    )