from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pypdf import PdfReader
import docx
from typing import List, Dict, Any
from collections import Counter
import re
import io
import json
from datetime import datetime
from bson.objectid import ObjectId
from database import collection

# ---------------------------------------------------------
#   FASTAPI APP CONFIGURATION
# ---------------------------------------------------------

app = FastAPI(
    title="Content Ingestion & Structured Output API by Sai Akhil",
    description="Upload PDF/DOCX/TXT → Generates Summary, Flashcards, Topics, Concept Graph → Stores in MongoDB",
    version="1.0.0"
)

# CORS so frontend (index.html) can call backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
#   TEXT PROCESSING UTILITIES
# ---------------------------------------------------------

STOPWORDS = set("""
a an the and or but if while with without to of in on for from at by as is are was were be being been
this that those these it its into such so than then too very can could should would may might will
just about over under up down out off
""".split())

def clean_text(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def split_sentences(text: str) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]

def tokenize(text: str) -> List[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = text.split()
    return [t for t in tokens if t not in STOPWORDS and len(t) > 2]


# ---------------------------------------------------------
#   FEATURE GENERATORS
# ---------------------------------------------------------

def generate_summary(text: str, max_sentences: int = 5) -> str:
    text = clean_text(text)
    sentences = split_sentences(text)

    if not sentences:
        return ""

    tokens = tokenize(text)
    if not tokens:
        return " ".join(sentences[:max_sentences])

    freq = Counter(tokens)
    sentence_scores = []

    for s in sentences:
        s_tokens = tokenize(s)
        score = sum(freq.get(t, 0) for t in s_tokens) / (len(s_tokens) + 1)
        sentence_scores.append((score, s))

    sentence_scores.sort(key=lambda x: x[0], reverse=True)
    top_sentences = [s for _, s in sentence_scores[:max_sentences]]

    ordered = [s for s in sentences if s in top_sentences]
    return " ".join(ordered)

def extract_keywords(text: str, top_k: int = 10) -> List[str]:
    tokens = tokenize(text)
    freq = Counter(tokens)
    return [w for w, _ in freq.most_common(top_k)]

def generate_flashcards(text: str, max_cards: int = 10) -> List[Dict[str, str]]:
    text = clean_text(text)
    sentences = split_sentences(text)
    keywords = extract_keywords(text, top_k=max_cards * 2)

    flashcards = []
    used = set()

    for kw in keywords:
        if kw in used:
            continue

        for s in sentences:
            if re.search(rf"\b{kw}\b", s, re.IGNORECASE):
                flashcards.append({
                    "question": f"What is '{kw}' in the context of this material?",
                    "answer": s
                })
                used.add(kw)
                break

        if len(flashcards) >= max_cards:
            break

    if not flashcards and sentences:
        flashcards.append({
            "question": "What is the main idea of this content?",
            "answer": sentences[0]
        })

    return flashcards

def extract_topics(text: str, max_topics: int = 8) -> Dict[str, Any]:
    keywords = extract_keywords(text, top_k=max_topics)
    if not keywords:
        return {"main_topic": "N/A", "subtopics": []}

    return {
        "main_topic": keywords[0],
        "subtopics": keywords[1:]
    }

def generate_concept_graph(text: str, max_nodes: int = 8) -> Dict[str, Any]:
    topics = extract_topics(text, max_topics=max_nodes)
    main = topics["main_topic"]
    subs = topics["subtopics"]

    nodes = list({main, *subs})
    links = [{"from": main, "to": sub} for sub in subs]

    return {"nodes": nodes, "links": links}


# ---------------------------------------------------------
#   FILE PARSING
# ---------------------------------------------------------

def extract_text_from_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    text = ""
    for page in reader.pages:
        text += (page.extract_text() or "") + "\n"
    return text

def extract_text_from_docx(file_bytes: bytes) -> str:
    stream = io.BytesIO(file_bytes)
    doc_file = docx.Document(stream)
    return "\n".join(para.text for para in doc_file.paragraphs)

def extract_text_from_txt(file_bytes: bytes) -> str:
    return file_bytes.decode("utf-8", errors="ignore")


# ---------------------------------------------------------
#   API ENDPOINTS
# ---------------------------------------------------------

@app.get("/")
def root():
    return {
        "message": "Content Ingestion API Running Successfully!",
        "upload_endpoint": "/ingest",
        "history_endpoint": "/history"
    }


# ------------------ MAIN INGEST ENDPOINT ---------------------

@app.post("/ingest")
async def ingest(file: UploadFile = File(...)):

    filename = file.filename.lower()
    file_bytes = await file.read()

    # Identify file type
    if filename.endswith(".pdf"):
        raw_text = extract_text_from_pdf(file_bytes)
    elif filename.endswith(".docx"):
        raw_text = extract_text_from_docx(file_bytes)
    elif filename.endswith(".txt"):
        raw_text = extract_text_from_txt(file_bytes)
    else:
        raise HTTPException(400, "Unsupported file type. Upload PDF, DOCX, or TXT.")

    if not raw_text.strip():
        raise HTTPException(422, "Unable to extract text from file.")

    # Generate outputs
    summary = generate_summary(raw_text)
    flashcards = generate_flashcards(raw_text)
    topics = extract_topics(raw_text)
    concept_graph = generate_concept_graph(raw_text)

    # Save to MongoDB
    record = {
        "filename": filename,
        "raw_text": raw_text,
        "summary": summary,
        "flashcards": flashcards,
        "topics": topics,
        "concept_graph": concept_graph,
        "created_at": datetime.utcnow()
    }

    inserted = collection.insert_one(record)
    record["_id"] = str(inserted.inserted_id)

    return record


# ------------------ GET ALL HISTORY ---------------------

@app.get("/history")
def get_history():
    results = []
    for item in collection.find().sort("created_at", -1):
        item["_id"] = str(item["_id"])
        results.append(item)
    return results


# ------------------ GET A SINGLE RECORD ---------------------

@app.get("/result/{id}")
def get_result(id: str):
    try:
        item = collection.find_one({"_id": ObjectId(id)})
    except:
        return {"error": "Invalid ID format"}

    if not item:
        return {"error": "Record not found"}

    item["_id"] = str(item["_id"])
    return item
