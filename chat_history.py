import sqlite3
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import os


class ChatHistoryManager:
    def __init__(self, db_path: str = "./chat_history.db"):
        self.db_path = db_path
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                retrieved_docs TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)
        """)

        conn.commit()
        conn.close()

    def create_user(self, username: str) -> str:
        user_id = str(uuid.uuid4())
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            cursor.execute("SELECT user_id FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            user_id = row["user_id"] if row else user_id
        finally:
            conn.close()
        return user_id

    def get_user(self, username: str) -> Optional[Dict]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def create_session(self, user_id: str, session_name: Optional[str] = None) -> str:
        session_id = str(uuid.uuid4())
        session_name = session_name or f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (session_id, user_id, session_name) VALUES (?, ?, ?)",
            (session_id, user_id, session_name)
        )
        conn.commit()
        conn.close()
        return session_id

    def list_sessions(self, user_id: str) -> List[Dict]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY last_updated DESC",
            (user_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        retrieved_docs: Optional[str] = None
    ) -> str:
        message_id = str(uuid.uuid4())
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO messages (message_id, session_id, role, content, retrieved_docs)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, session_id, role, content, retrieved_docs)
        )
        cursor.execute(
            "UPDATE sessions SET last_updated = CURRENT_TIMESTAMP WHERE session_id = ?",
            (session_id,)
        )
        conn.commit()
        conn.close()
        return message_id

    def get_session_history(
        self,
        session_id: str,
        limit: Optional[int] = None,
        include_docs: bool = False
    ) -> List[Tuple[str, str]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        query = "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC"
        params: List = [session_id]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return [(row["role"], row["content"]) for row in rows]

    def get_session_history_as_langchain(self, session_id: str, limit: Optional[int] = None):
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

        history = self.get_session_history(session_id, limit=limit)
        messages = []
        for role, content in history:
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def delete_session(self, session_id: str) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()

    def delete_user(self, user_id: str) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    def get_stats(self) -> Dict:
        conn = self._get_connection()
        cursor = conn.cursor()
        stats = {}
        cursor.execute("SELECT COUNT(*) as count FROM users")
        stats["total_users"] = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) as count FROM sessions")
        stats["total_sessions"] = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) as count FROM messages")
        stats["total_messages"] = cursor.fetchone()["count"]
        conn.close()
        return stats
