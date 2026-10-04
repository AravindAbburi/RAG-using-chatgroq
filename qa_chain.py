import os
import logging
import json
from typing import Dict, List, Optional, Any, Tuple
from dotenv import load_dotenv

from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_core.messages import BaseMessage
from langchain_groq import ChatGroq

from rag_pipeline import RAGPipeline
from chat_history import ChatHistoryManager

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


RAG_SYSTEM_PROMPT = """You are an expert assistant for question-answering tasks.
Use the following pieces of retrieved context to answer the user's question.
If you don't know the answer, just say that you don't know, don't try to make up an answer.
Keep the answer concise and accurate, with citations where possible.

IMPORTANT RULES:
1. Only use information from the provided context to answer the question.
2. If the context does not contain enough information, explicitly state that.
3. Reference the source of information when possible.
4. Structure complex answers with clear sections if needed.
5. Do NOT mention the word "context" or "documents" in your answer; just provide the information naturally.

Retrieved Context:
{context}
"""

QUERY_REWRITE_PROMPT = """You are a query rewriting expert.
Given the conversation history and a follow-up question, rewrite the follow-up question
to be a standalone, detailed question that captures all necessary context.
If the question is already standalone and clear, return it unchanged.

Conversation History:
{chat_history}

Follow-up Input: {question}

Standalone Question:"""

SELF_RAG_PROMPT = """You are a critical evaluator. Given the question, retrieved context, and generated answer,
determine if the answer is fully supported by the context.
If the answer is not fully supported or hallucinates information, respond with: "HALLUCINATE"
If the answer is good and grounded in the context, respond with: "VERIFIED"

Question: {question}

Context: {context}

Answer: {answer}

Evaluation:"""


class RAGQASystem:
    def __init__(
        self,
        rag_pipeline: Optional[RAGPipeline] = None,
        chat_history_manager: Optional[ChatHistoryManager] = None,
        llm_model_name: Optional[str] = None,
        temperature: Optional[float] = None,
        groq_api_key: Optional[str] = None,
    ):
        self.rag_pipeline = rag_pipeline or RAGPipeline()
        self.chat_history = chat_history_manager or ChatHistoryManager(
            db_path=os.getenv("CHAT_HISTORY_DB", "./chat_history.db")
        )

        self.llm_model_name = llm_model_name or os.getenv(
            "LLM_MODEL_NAME", "llama-3.1-70b-versatile"
        )
        self.temperature = temperature or float(os.getenv("TEMPERATURE", "0.1"))

        groq_api_key = groq_api_key or os.getenv("GROQ_API_KEY")
        if not groq_api_key:
            raise ValueError(
                "GROQ_API_KEY not found. Please set it in your .env file."
            )

        self.llm = ChatGroq(
            model=self.llm_model_name,
            temperature=self.temperature,
            groq_api_key=groq_api_key,
        )

        self.qa_chain = self._build_qa_chain()
        self.rewrite_chain = self._build_rewrite_chain()
        self.verification_chain = self._build_verification_chain()

    @staticmethod
    def format_docs(docs: List[Document]) -> str:
        formatted = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get("source", "unknown")
            page = doc.metadata.get("page", "")
            location = f" (Page {page})" if page else ""
            metadata_str = json.dumps(
                {k: v for k, v in doc.metadata.items() if k != "row_id"},
                ensure_ascii=False,
            )
            formatted.append(
                f"[Source {i}] {source}{location}\n"
                f"Metadata: {metadata_str}\n"
                f"Content:\n{doc.page_content}\n"
            )
        return "\n---\n\n".join(formatted)

    @staticmethod
    def format_chat_history(history: List[Tuple[str, str]], max_turns: int = 10) -> str:
        recent = history[-max_turns:] if len(history) > max_turns else history
        formatted = []
        for role, content in recent:
            prefix = "User" if role == "user" else "Assistant"
            formatted.append(f"{prefix}: {content}")
        return "\n".join(formatted) if formatted else "No history"

    def _build_qa_chain(self):
        prompt = ChatPromptTemplate.from_messages([
            ("system", RAG_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="chat_history_list"),
            ("human", "{question}"),
        ])

        retrieve = RunnableLambda(self._retrieve_step)

        chain = (
            {
                "question": lambda x: x["standalone_question"],
                "chat_history_list": lambda x: x.get("chat_history_list", []),
                "context": retrieve | self.format_docs,
                "_raw_docs": retrieve,
            }
            | prompt
            | self.llm
            | StrOutputParser()
        )
        return chain

    def _retrieve_step(self, inputs: Dict[str, Any]) -> List[Document]:
        query = inputs.get("standalone_question") or inputs.get("question", "")
        try:
            docs = self.rag_pipeline.retrieve(query)
            return docs
        except Exception as e:
            logger.error(f"Retrieval failed: {e}")
            return []

    def _build_rewrite_chain(self):
        prompt = ChatPromptTemplate.from_template(QUERY_REWRITE_PROMPT)
        return prompt | self.llm | StrOutputParser()

    def _build_verification_chain(self):
        prompt = ChatPromptTemplate.from_template(SELF_RAG_PROMPT)
        return prompt | self.llm | StrOutputParser()

    def rewrite_query_if_needed(
        self,
        question: str,
        chat_history: List[Tuple[str, str]],
    ) -> str:
        if not chat_history:
            return question

        has_references = any(
            ref in question.lower()
            for ref in ["this", "that", "these", "those", "it", "its", "they", "them"]
        )
        if not has_references and len(question.split()) >= 5:
            return question

        try:
            formatted_history = self.format_chat_history(chat_history)
            rewritten = self.rewrite_chain.invoke({
                "chat_history": formatted_history,
                "question": question,
            })
            rewritten = rewritten.strip()
            if rewritten:
                logger.info(f"Query rewritten: '{question}' -> '{rewritten}'")
                return rewritten
        except Exception as e:
            logger.warning(f"Query rewrite failed: {e}, using original")

        return question

    def _verify_answer(
        self, question: str, context: str, answer: str
    ) -> Tuple[str, bool]:
        try:
            result = self.verification_chain.invoke({
                "question": question,
                "context": context,
                "answer": answer,
            }).strip().upper()
            is_verified = "VERIFIED" in result
            return result, is_verified
        except Exception as e:
            logger.warning(f"Verification failed: {e}")
            return "ERROR", True

    def _compress_context_if_needed(
        self, docs: List[Document], question: str
    ) -> List[Document]:
        if len(docs) <= self.rag_pipeline.top_k:
            return docs

        try:
            from langchain_core.prompts import PromptTemplate

            extract_prompt = PromptTemplate.from_template(
                """Given the question, extract and keep only the sentences from the
                document that are relevant to answering the question.
                Return the relevant text verbatim. If nothing is relevant, return "NOT_RELEVANT".

                Question: {question}
                Document: {doc_content}
                Relevant extract:"""
            )
            extract_chain = extract_prompt | self.llm | StrOutputParser()

            compressed = []
            for doc in docs:
                result = extract_chain.invoke({
                    "question": question,
                    "doc_content": doc.page_content,
                }).strip()
                if result and result != "NOT_RELEVANT":
                    new_doc = Document(
                        page_content=result,
                        metadata={**doc.metadata, "compressed": True},
                    )
                    compressed.append(new_doc)
            if compressed:
                logger.info(f"Context compressed: {len(docs)} -> {len(compressed)} docs")
                return compressed
        except Exception as e:
            logger.warning(f"Context compression failed: {e}")

        return docs

    def ask(
        self,
        question: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        username: Optional[str] = None,
        enable_query_rewrite: bool = True,
        enable_verification: bool = True,
        enable_compression: bool = False,
        return_sources: bool = True,
    ) -> Dict[str, Any]:
        if not question.strip():
            raise ValueError("Question cannot be empty.")

        if username and not user_id:
            user_id = self.chat_history.create_user(username)

        if user_id and not session_id:
            session_id = self.chat_history.create_session(user_id)

        history: List[Tuple[str, str]] = []
        history_langchain: List[BaseMessage] = []
        if session_id:
            history = self.chat_history.get_session_history(session_id)
            history_langchain = self.chat_history.get_session_history_as_langchain(
                session_id, limit=10
            )

        standalone_question = question
        if enable_query_rewrite:
            standalone_question = self.rewrite_query_if_needed(question, history)

        retrieved_docs = self.rag_pipeline.retrieve(standalone_question)

        if enable_compression and len(retrieved_docs) > 2:
            retrieved_docs = self._compress_context_if_needed(
                retrieved_docs, standalone_question
            )

        answer = self.qa_chain.invoke({
            "question": question,
            "standalone_question": standalone_question,
            "chat_history_list": history_langchain,
        })

        is_verified = True
        verification_result = "SKIPPED"
        if enable_verification and retrieved_docs:
            ctx_str = self.format_docs(retrieved_docs)
            verification_result, is_verified = self._verify_answer(
                standalone_question, ctx_str, answer
            )
            if not is_verified:
                answer = (
                    f"[Note: Answer may contain unsupported information]\n\n{answer}"
                )

        retrieved_docs_json = json.dumps([
            {"content": d.page_content[:500], "metadata": d.metadata}
            for d in retrieved_docs
        ], ensure_ascii=False)

        if session_id:
            self.chat_history.add_message(
                session_id=session_id,
                role="user",
                content=question,
            )
            self.chat_history.add_message(
                session_id=session_id,
                role="assistant",
                content=answer,
                retrieved_docs=retrieved_docs_json,
            )

        result: Dict[str, Any] = {
            "question": question,
            "standalone_question": standalone_question,
            "answer": answer,
            "verification": verification_result,
            "is_verified": is_verified,
            "session_id": session_id,
            "user_id": user_id,
        }

        if return_sources:
            result["sources"] = [
                {
                    "content": doc.page_content,
                    "metadata": doc.metadata,
                }
                for doc in retrieved_docs
            ]

        return result

    def stream_ask(
        self,
        question: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        username: Optional[str] = None,
        enable_query_rewrite: bool = True,
        return_sources: bool = True,
    ):
        if not question.strip():
            raise ValueError("Question cannot be empty.")

        if username and not user_id:
            user_id = self.chat_history.create_user(username)

        if user_id and not session_id:
            session_id = self.chat_history.create_session(user_id)

        history: List[Tuple[str, str]] = []
        history_langchain: List[BaseMessage] = []
        if session_id:
            history = self.chat_history.get_session_history(session_id)
            history_langchain = self.chat_history.get_session_history_as_langchain(
                session_id, limit=10
            )

        standalone_question = question
        if enable_query_rewrite:
            standalone_question = self.rewrite_query_if_needed(question, history)
            yield {"type": "rewrite", "data": standalone_question}

        retrieved_docs = self.rag_pipeline.retrieve(standalone_question)
        if return_sources:
            yield {
                "type": "sources",
                "data": [
                    {"content": d.page_content[:300], "metadata": d.metadata}
                    for d in retrieved_docs
                ],
            }

        full_answer = ""
        for chunk in self.qa_chain.stream({
            "question": question,
            "standalone_question": standalone_question,
            "chat_history_list": history_langchain,
        }):
            full_answer += chunk
            yield {"type": "token", "data": chunk}

        if session_id:
            self.chat_history.add_message(session_id, "user", question)
            self.chat_history.add_message(session_id, "assistant", full_answer)

        yield {
            "type": "done",
            "data": {
                "question": question,
                "standalone_question": standalone_question,
                "answer": full_answer,
                "session_id": session_id,
                "user_id": user_id,
            },
        }

    def evaluate_retrieval_precision(
        self,
        test_queries: List[Dict[str, Any]],
    ) -> Dict[str, float]:
        """
        Evaluate retrieval precision. Each test query should be:
        {"question": "...", "expected_keywords": ["kw1", "kw2"]}
        """
        total = len(test_queries)
        relevant_count = 0
        total_docs_retrieved = 0
        total_relevant_in_top = 0

        for item in test_queries:
            question = item["question"]
            expected_keywords = item.get("expected_keywords", [])

            docs = self.rag_pipeline.retrieve(question)
            total_docs_retrieved += len(docs)

            query_relevant = False
            for doc in docs:
                content_lower = doc.page_content.lower()
                doc_relevant = any(
                    kw.lower() in content_lower for kw in expected_keywords
                )
                if doc_relevant:
                    total_relevant_in_top += 1
                    query_relevant = True

            if query_relevant or not expected_keywords:
                relevant_count += 1

        return {
            "query_coverage": relevant_count / max(total, 1),
            "precision_at_k": total_relevant_in_top / max(total_docs_retrieved, 1),
            "avg_docs_per_query": total_docs_retrieved / max(total, 1),
            "total_queries": total,
        }
