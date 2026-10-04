import os
import sys
import json
import logging
import argparse
from dotenv import load_dotenv

from rag_pipeline import RAGPipeline
from qa_chain import RAGQASystem
from chat_history import ChatHistoryManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def print_separator(title: str = "") -> None:
    width = 70
    if title:
        line = f" {title} ".center(width, "=")
    else:
        line = "=" * width
    print(f"\n{line}\n")


def setup_pipeline_from_huggingface(args) -> RAGPipeline:
    print_separator("STEP 1: Initializing RAG Pipeline")
    pipeline = RAGPipeline()
    stats = pipeline.get_vectorstore_stats()
    print(f"Current vector store status: {stats}")

    if stats.get("document_count", 0) > 0 and not args.force_reindex:
        print(f"Using existing vector store with {stats['document_count']} documents.")
        print("Use --force-reindex to re-ingest data.")
        return pipeline

    print_separator("STEP 2: Loading Dataset from HuggingFace")
    dataset_name = args.dataset or os.getenv("DATASET_NAME", "rag-datasets/rag-mini-wikipedia")
    split = args.split or os.getenv("DATASET_SPLIT", "passages")

    print(f"Dataset: {dataset_name}")
    print(f"Split:   {split}")
    if args.max_samples:
        print(f"Limit:   {args.max_samples} samples")

    documents = pipeline.load_huggingface_dataset(
        dataset_name=dataset_name,
        split=split,
        max_samples=args.max_samples,
    )

    if not documents:
        logger.error("No documents loaded. Exiting.")
        sys.exit(1)

    print(f"\nLoaded {len(documents)} documents successfully!")
    print(f"First document preview ({min(len(documents[0].page_content), 300)} chars):")
    print(f"  \"{documents[0].page_content[:300]}...\"")
    print(f"  Metadata: {json.dumps(documents[0].metadata, indent=4, default=str)}")

    print_separator("STEP 3: Ingesting Documents into ChromaDB")
    pipeline.ingest_documents(documents, split=True)

    final_stats = pipeline.get_vectorstore_stats()
    print(f"Vector store ready: {json.dumps(final_stats, indent=4)}")

    return pipeline


def test_retrieval_only(pipeline: RAGPipeline, queries_file: str = None) -> None:
    print_separator("STEP 4: Testing Retrieval Precision")

    test_queries = [
        {
            "question": "What is machine learning and how does it work?",
            "expected_keywords": ["machine", "learning", "algorithm", "data", "model", "train"],
        },
        {
            "question": "Explain quantum computing basics",
            "expected_keywords": ["quantum", "qubit", "superposition", "computing"],
        },
        {
            "question": "How does photosynthesis work in plants?",
            "expected_keywords": ["photosynthesis", "plant", "light", "chlorophyll", "energy", "carbon"],
        },
        {
            "question": "What are the main causes of climate change?",
            "expected_keywords": ["climate", "warming", "greenhouse", "carbon", "emission", "fossil"],
        },
        {
            "question": "Explain the theory of relativity by Einstein",
            "expected_keywords": ["relativity", "einstein", "gravity", "space", "time", "theory"],
        },
    ]

    if queries_file and os.path.exists(queries_file):
        try:
            with open(queries_file, "r") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    test_queries = loaded
                    print(f"Loaded {len(test_queries)} test queries from {queries_file}")
        except Exception as e:
            logger.warning(f"Could not load queries file: {e}")

    print(f"Testing with {len(test_queries)} queries...\n")

    for i, item in enumerate(test_queries, 1):
        q = item["question"]
        print(f"Q{i}: {q}")
        print("-" * 50)

        docs = pipeline.retrieve(q, k=3)
        for j, doc in enumerate(docs, 1):
            src = doc.metadata.get("source", "unknown")
            preview = doc.page_content[:200].replace("\n", " ")
            print(f"  [{j}] {src}: {preview}...")

        scores = pipeline.retrieve_with_scores(q, k=3)
        print(f"\n  Relevance Scores: {[f'{s:.3f}' for _, s in scores]}")
        print()

    metrics = pipeline.evaluate_retrieval_precision(test_queries) if hasattr(
        pipeline, "evaluate_retrieval_precision"
    ) else None

    print("\nRetrieval Metrics:")
    if metrics:
        for k, v in metrics.items():
            if isinstance(v, float):
                print(f"  {k}: {v:.4f} ({v*100:.1f}%)" if "precision" in k or "coverage" in k else f"  {k}: {v:.4f}")
            else:
                print(f"  {k}: {v}")


def interactive_qa_mode(system: RAGQASystem, username: str = "demo_user") -> None:
    print_separator("STEP 5: Interactive QA Mode")
    print(f"Logged in as user: {username}")
    print("Type your questions below. Type 'exit', 'quit', or 'sessions' for options.")
    print("Type 'sources' after an answer to see retrieved documents.\n")

    user_id = system.chat_history.create_user(username)
    sessions = system.chat_history.list_sessions(user_id)

    if sessions:
        print(f"Found {len(sessions)} existing session(s):")
        for i, s in enumerate(sessions, 1):
            print(f"  [{i}] {s['session_name']} (ID: {s['session_id'][:12]}...)")
        choice = input("\nSelect session number or press Enter for new: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            session_id = sessions[int(choice) - 1]["session_id"]
            print(f"Resumed session: {sessions[int(choice)-1]['session_name']}")
            hist = system.chat_history.get_session_history(session_id)
            if hist:
                print(f"\nConversation history ({len(hist)} messages):")
                for role, content in hist[-6:]:
                    prefix = "You" if role == "user" else "AI"
                    print(f"  {prefix}: {content[:200]}{'...' if len(content) > 200 else ''}")
        else:
            session_id = system.chat_history.create_session(
                user_id, session_name="New Interactive Session"
            )
    else:
        session_id = system.chat_history.create_session(
            user_id, session_name="Default Session"
        )

    last_sources = []

    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question:
            continue

        if question.lower() in ("exit", "quit", "bye"):
            print("Goodbye!")
            break

        if question.lower() == "sessions":
            sessions = system.chat_history.list_sessions(user_id)
            print(f"\nYour sessions ({len(sessions)}):")
            for i, s in enumerate(sessions, 1):
                print(f"  [{i}] {s['session_name']} | Last: {s['last_updated']}")
            switch = input("Switch to (number), 'new' for new, or Enter to cancel: ").strip()
            if switch.isdigit() and 1 <= int(switch) <= len(sessions):
                session_id = sessions[int(switch) - 1]["session_id"]
                print(f"Switched to: {sessions[int(switch)-1]['session_name']}")
            elif switch.lower() == "new":
                name = input("Session name (optional): ").strip() or None
                session_id = system.chat_history.create_session(user_id, session_name=name)
                print(f"Created new session: {session_id[:12]}...")
            continue

        if question.lower() == "sources":
            if last_sources:
                print(f"\n--- Retrieved Sources ({len(last_sources)}) ---")
                for i, src in enumerate(last_sources, 1):
                    meta = src.get("metadata", {})
                    print(f"\n[{i}] Source: {meta.get('source', 'unknown')}")
                    if meta.get("page"):
                        print(f"    Page: {meta['page']}")
                    content = src.get("content", "")
                    print(f"    Content: {content[:500]}{'...' if len(content) > 500 else ''}")
            else:
                print("No sources cached yet. Ask a question first.")
            continue

        if question.lower() == "stats":
            print(f"\nChat Stats: {json.dumps(system.chat_history.get_stats(), indent=4)}")
            print(f"Vector Store: {json.dumps(system.rag_pipeline.get_vectorstore_stats(), indent=4)}")
            continue

        print("\nAI: ", end="", flush=True)

        full_answer = ""
        for event in system.stream_ask(
            question=question,
            user_id=user_id,
            session_id=session_id,
            enable_query_rewrite=True,
            return_sources=True,
        ):
            if event["type"] == "token":
                print(event["data"], end="", flush=True)
                full_answer += event["data"]
            elif event["type"] == "rewrite" and event["data"] != question:
                print(f"\n  (Rewritten query: {event['data']})", end="\n\n")
            elif event["type"] == "sources":
                last_sources = event["data"]
            elif event["type"] == "done":
                data = event["data"]
                if not data.get("is_verified", True):
                    print(f"\n  [Verification: {data.get('verification', 'UNKNOWN')}]")

        print()


def demo_quickstart() -> None:
    print_separator("RAG QUICKSTART DEMO")
    print("This demo will:")
    print("  1. Initialize the RAG pipeline with HuggingFace embeddings")
    print("  2. Load a small dataset (or use existing vector store)")
    print("  3. Run retrieval tests to check precision")
    print("  4. Launch interactive QA with multi-user chat history\n")

    if not os.getenv("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY is not set!")
        print("Please copy .env.example to .env and add your GROQ API key.")
        print("\nGet your free key: https://console.groq.com/keys")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="RAG System with ChatGroq - Retrieval Augmented Generation Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full interactive mode (recommended)
  python main.py --interactive --username myuser

  # Ingest data only (build vector store)
  python main.py --ingest-only --dataset rag-datasets/mini-wikipedia --max-samples 500

  # Test retrieval quality only
  python main.py --test-retrieval

  # Force re-index with a different dataset
  python main.py --force-reindex --dataset squad --split train --max-samples 2000
        """,
    )

    parser.add_argument("--interactive", action="store_true", help="Launch interactive QA mode")
    parser.add_argument("--ingest-only", action="store_true", help="Only ingest data and build vector store")
    parser.add_argument("--test-retrieval", action="store_true", help="Run retrieval precision tests")
    parser.add_argument("--force-reindex", action="store_true", help="Force re-ingestion even if store exists")
    parser.add_argument("--username", type=str, default="demo_user", help="Username for chat history")
    parser.add_argument("--dataset", type=str, help="HuggingFace dataset name")
    parser.add_argument("--split", type=str, help="Dataset split (train/validation/test)")
    parser.add_argument("--max-samples", type=int, help="Max number of samples to load")
    parser.add_argument("--queries-file", type=str, help="JSON file with test queries for evaluation")

    args = parser.parse_args()

    if len(sys.argv) == 1:
        args.interactive = True

    demo_quickstart()

    pipeline = setup_pipeline_from_huggingface(args)

    if args.ingest_only:
        print("\nData ingestion complete. Vector store is ready to use!")
        stats = pipeline.get_vectorstore_stats()
        print(json.dumps(stats, indent=4))
        return

    if args.test_retrieval:
        test_retrieval_only(pipeline, args.queries_file)
        return

    print_separator("STEP 5: Initializing QA System with ChatGroq")
    qa_system = RAGQASystem(rag_pipeline=pipeline)
    print(f"LLM Model:    {qa_system.llm_model_name}")
    print(f"Temperature:  {qa_system.temperature}")
    print(f"Chat DB:      {qa_system.chat_history.db_path}")
    print(f"QA chain initialized successfully.")

    stats = qa_system.chat_history.get_stats()
    print(f"\nChat history stats: {json.dumps(stats)}")

    sample_questions = [
        "What is the definition of artificial intelligence?",
        "How does neural network deep learning work?",
        "What are the benefits of renewable energy?",
    ]
    print(f"\n--- Example QA Run (3 sample questions) ---")
    user_id = qa_system.chat_history.create_user("sample_run")
    session_id = qa_system.chat_history.create_session(user_id, "Sample Run Session")

    for q in sample_questions:
        print(f"\nQ: {q}")
        result = qa_system.ask(
            question=q,
            user_id=user_id,
            session_id=session_id,
            enable_query_rewrite=True,
            enable_verification=True,
            return_sources=False,
        )
        print(f"A: {result['answer']}")
        print(f"   [Rewritten: {result['standalone_question'][:80]}...]")
        print(f"   [Verified: {result['is_verified']} | {result['verification']}]")

    if args.interactive:
        interactive_qa_mode(qa_system, username=args.username)

    print_separator("SESSION COMPLETE")
    print(f"Final chat stats: {json.dumps(qa_system.chat_history.get_stats(), indent=4)}")
    print("Thank you for using the RAG QA System!")


if __name__ == "__main__":
    main()
