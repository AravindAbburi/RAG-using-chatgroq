import os
import logging
from typing import List, Optional, Any
from dotenv import load_dotenv

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from datasets import load_dataset, Dataset
from pypdf import PdfReader

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RAGPipeline:
    def __init__(
        self,
        persist_directory: Optional[str] = None,
        embedding_model_name: Optional[str] = None,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        top_k: Optional[int] = None,
    ):
        self.persist_directory = persist_directory or os.getenv(
            "CHROMA_PERSIST_DIRECTORY", "./chroma_db"
        )
        self.embedding_model_name = embedding_model_name or os.getenv(
            "EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2"
        )
        self.chunk_size = chunk_size or int(os.getenv("MAX_CHUNK_SIZE", "1000"))
        self.chunk_overlap = chunk_overlap or int(os.getenv("CHUNK_OVERLAP", "200"))
        self.top_k = top_k or int(os.getenv("TOP_K_RETRIEVE", "4"))

        self.embeddings = self._init_embeddings()
        self.vectorstore: Optional[Chroma] = None
        self.retriever = None
        self._init_vectorstore()

    def _init_embeddings(self) -> HuggingFaceEmbeddings:
        logger.info(f"Loading embedding model: {self.embedding_model_name}")
        model_kwargs = {"device": "cpu"}
        encode_kwargs = {"normalize_embeddings": True}
        return HuggingFaceEmbeddings(
            model_name=self.embedding_model_name,
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
        )

    def _init_vectorstore(self) -> None:
        if os.path.exists(self.persist_directory) and os.listdir(self.persist_directory):
            logger.info(f"Loading existing vector store from {self.persist_directory}")
            self.vectorstore = Chroma(
                persist_directory=self.persist_directory,
                embedding_function=self.embeddings,
            )
            self._configure_retriever()
            logger.info("Vector store loaded successfully")
        else:
            logger.info("No existing vector store found. Ready to ingest data.")

    def _configure_retriever(self) -> None:
        if self.vectorstore:
            self.retriever = self.vectorstore.as_retriever(
                search_type="mmr",
                search_kwargs={
                    "k": self.top_k,
                    "fetch_k": max(20, self.top_k * 5),
                    "lambda_mult": 0.7,
                },
            )

    def load_huggingface_dataset(
        self,
        dataset_name: Optional[str] = None,
        split: Optional[str] = None,
        text_columns: Optional[List[str]] = None,
        max_samples: Optional[int] = None,
    ) -> List[Document]:
        dataset_name = dataset_name or os.getenv("DATASET_NAME", "rag-datasets/rag-mini-wikipedia")
        split = split or os.getenv("DATASET_SPLIT", "passages")
        dataset_config = os.getenv("DATASET_CONFIG", "text-corpus")

        logger.info(f"Loading dataset: {dataset_name} (config: {dataset_config}, split: {split})")

        hf_token = os.getenv("HF_TOKEN")
        load_kwargs = {"path": dataset_name, "split": split}
        if dataset_config:
            load_kwargs["name"] = dataset_config
        if hf_token and hf_token != "your_huggingface_token_here":
            load_kwargs["token"] = hf_token

        dataset: Dataset = load_dataset(**load_kwargs)

        if max_samples and len(dataset) > max_samples:
            logger.info(f"Limiting to {max_samples} samples from {len(dataset)} total")
            dataset = dataset.select(range(max_samples))

        if text_columns is None:
            text_columns = self._detect_text_columns(dataset)

        logger.info(f"Using text columns: {text_columns}")

        documents: List[Document] = []
        for idx, row in enumerate(dataset):
            content_parts = []
            metadata = {"source": dataset_name, "row_id": idx}
            for col in dataset.column_names:
                if col in text_columns:
                    content_parts.append(str(row[col]))
                else:
                    try:
                        metadata[col] = str(row[col])[:500]
                    except Exception:
                        pass
            content = "\n\n".join(content_parts).strip()
            if content:
                documents.append(Document(page_content=content, metadata=metadata))

        logger.info(f"Loaded {len(documents)} documents from dataset")
        return documents

    @staticmethod
    def _detect_text_columns(dataset: Dataset) -> List[str]:
        text_columns = []
        for col, dtype in dataset.features.items():
            dtype_str = str(dtype)
            if dtype_str == "string" or "Value" in dtype_str:
                text_columns.append(col)
        if not text_columns:
            text_columns = [dataset.column_names[0]]
        return text_columns

    def load_pdf_files(self, pdf_dir: str) -> List[Document]:
        documents: List[Document] = []
        if not os.path.exists(pdf_dir):
            logger.warning(f"PDF directory not found: {pdf_dir}")
            return documents

        for filename in os.listdir(pdf_dir):
            if filename.lower().endswith(".pdf"):
                filepath = os.path.join(pdf_dir, filename)
                logger.info(f"Processing PDF: {filename}")
                try:
                    reader = PdfReader(filepath)
                    for page_num, page in enumerate(reader.pages):
                        text = page.extract_text() or ""
                        if text.strip():
                            documents.append(
                                Document(
                                    page_content=text,
                                    metadata={
                                        "source": filename,
                                        "page": page_num + 1,
                                        "type": "pdf",
                                    },
                                )
                            )
                except Exception as e:
                    logger.error(f"Error reading {filename}: {e}")

        logger.info(f"Loaded {len(documents)} pages from {len([f for f in os.listdir(pdf_dir) if f.lower().endswith('.pdf')])} PDF files")
        return documents

    def load_text_files(self, text_dir: str) -> List[Document]:
        documents: List[Document] = []
        if not os.path.exists(text_dir):
            logger.warning(f"Text directory not found: {text_dir}")
            return documents

        for filename in os.listdir(text_dir):
            if filename.lower().endswith((".txt", ".md")):
                filepath = os.path.join(text_dir, filename)
                logger.info(f"Processing file: {filename}")
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():
                        documents.append(
                            Document(
                                page_content=content,
                                metadata={
                                    "source": filename,
                                    "type": "text",
                                },
                            )
                        )
                except Exception as e:
                    logger.error(f"Error reading {filename}: {e}")

        logger.info(f"Loaded {len(documents)} text files")
        return documents

    def split_documents(self, documents: List[Document]) -> List[Document]:
        logger.info(
            f"Splitting {len(documents)} documents "
            f"(chunk_size={self.chunk_size}, overlap={self.chunk_overlap})"
        )
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            separators=[
                "\n\n",
                "\n",
                ". ",
                "! ",
                "? ",
                " ",
                "",
            ],
        )
        splits = text_splitter.split_documents(documents)
        logger.info(f"Created {len(splits)} document chunks")
        return splits

    def ingest_documents(
        self,
        documents: List[Document],
        split: bool = True,
    ) -> None:
        if split:
            documents = self.split_documents(documents)

        logger.info(f"Ingesting {len(documents)} chunks into vector store")
        batch_size = 100

        if self.vectorstore is None:
            for i in range(0, len(documents), batch_size):
                batch = documents[i : i + batch_size]
                if i == 0:
                    self.vectorstore = Chroma.from_documents(
                        documents=batch,
                        embedding=self.embeddings,
                        persist_directory=self.persist_directory,
                    )
                else:
                    self.vectorstore.add_documents(batch)
                logger.info(f"Ingested batch {i // batch_size + 1} ({min(i + batch_size, len(documents))}/{len(documents)})")
        else:
            for i in range(0, len(documents), batch_size):
                batch = documents[i : i + batch_size]
                self.vectorstore.add_documents(batch)
                logger.info(f"Ingested batch {i // batch_size + 1} ({min(i + batch_size, len(documents))}/{len(documents)})")

        self._configure_retriever()
        logger.info("Ingestion complete. Vector store persisted.")

    def retrieve(self, query: str, k: Optional[int] = None) -> List[Document]:
        if self.retriever is None:
            raise ValueError(
                "Vector store not initialized. Please ingest documents first."
            )

        k = k or self.top_k
        if k != self.top_k:
            temp_retriever = self.vectorstore.as_retriever(
                search_type="mmr",
                search_kwargs={
                    "k": k,
                    "fetch_k": max(20, k * 5),
                    "lambda_mult": 0.7,
                },
            )
            results = temp_retriever.invoke(query)
        else:
            results = self.retriever.invoke(query)

        logger.info(f"Retrieved {len(results)} documents for query: {query[:50]}...")
        return results

    def retrieve_with_scores(self, query: str, k: Optional[int] = None) -> List[tuple]:
        if self.vectorstore is None:
            raise ValueError(
                "Vector store not initialized. Please ingest documents first."
            )
        k = k or self.top_k
        results = self.vectorstore.similarity_search_with_relevance_scores(query, k=k)
        logger.info(f"Retrieved {len(results)} documents with scores")
        return results

    def get_vectorstore_stats(self) -> dict:
        if self.vectorstore is None:
            return {"status": "empty", "document_count": 0}
        try:
            count = self.vectorstore._collection.count()
        except Exception:
            count = -1
        return {
            "status": "loaded",
            "document_count": count,
            "persist_directory": self.persist_directory,
            "embedding_model": self.embedding_model_name,
            "top_k": self.top_k,
        }
