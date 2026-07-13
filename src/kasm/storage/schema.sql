PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    assembly_term INTEGER NOT NULL CHECK (assembly_term > 0),
    committee_id TEXT,
    committee_name_ko TEXT,
    committee_name_en TEXT,
    title TEXT NOT NULL,
    meeting_type TEXT NOT NULL,
    meeting_number TEXT,
    date TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_filters
    ON meetings (assembly_term, committee_id, date, meeting_type);

CREATE TABLE IF NOT EXISTS meeting_agendas (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    title TEXT NOT NULL,
    bill_no TEXT,
    official_url TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    UNIQUE (meeting_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_meeting_agendas_bill
    ON meeting_agendas (bill_no, meeting_id, sequence);
CREATE VIRTUAL TABLE IF NOT EXISTS meeting_agendas_fts USING fts5(
    title, bill_no, content='meeting_agendas', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS meeting_agendas_ai AFTER INSERT ON meeting_agendas BEGIN
    INSERT INTO meeting_agendas_fts(rowid, title, bill_no)
    VALUES (new.rowid, new.title, new.bill_no);
END;
CREATE TRIGGER IF NOT EXISTS meeting_agendas_ad AFTER DELETE ON meeting_agendas BEGIN
    INSERT INTO meeting_agendas_fts(meeting_agendas_fts, rowid, title, bill_no)
    VALUES ('delete', old.rowid, old.title, old.bill_no);
END;
CREATE TRIGGER IF NOT EXISTS meeting_agendas_au AFTER UPDATE ON meeting_agendas BEGIN
    INSERT INTO meeting_agendas_fts(meeting_agendas_fts, rowid, title, bill_no)
    VALUES ('delete', old.rowid, old.title, old.bill_no);
    INSERT INTO meeting_agendas_fts(rowid, title, bill_no)
    VALUES (new.rowid, new.title, new.bill_no);
END;

CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    name_ko TEXT NOT NULL,
    name_en TEXT
);

CREATE TABLE IF NOT EXISTS speeches (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    speaker_id TEXT REFERENCES persons(id) ON DELETE SET NULL,
    speaker_name TEXT NOT NULL,
    speaker_role TEXT,
    organization TEXT,
    text TEXT NOT NULL,
    agenda TEXT,
    previous_speech_id TEXT REFERENCES speeches(id) DEFERRABLE INITIALLY DEFERRED,
    next_speech_id TEXT REFERENCES speeches(id) DEFERRABLE INITIALLY DEFERRED,
    source_locator TEXT,
    source_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    UNIQUE (meeting_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_speeches_speaker ON speeches (speaker_id);
CREATE INDEX IF NOT EXISTS idx_speeches_filters
    ON speeches (speaker_name, speaker_role, organization);

CREATE TABLE IF NOT EXISTS bills (
    id TEXT PRIMARY KEY,
    bill_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    assembly_term INTEGER NOT NULL CHECK (assembly_term > 0),
    proposer TEXT,
    committee TEXT,
    proposed_at TEXT,
    process_result TEXT,
    processed_at TEXT,
    official_url TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bills_filters
    ON bills (assembly_term, committee, process_result, proposed_at);

CREATE TABLE IF NOT EXISTS bill_documents (
    id TEXT PRIMARY KEY,
    bill_id TEXT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL CHECK (document_type IN ('committee_review_report')),
    title TEXT NOT NULL,
    file_format TEXT NOT NULL CHECK (file_format IN ('pdf')),
    official_url TEXT NOT NULL,
    text TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE (bill_id, document_type, official_url)
);
CREATE INDEX IF NOT EXISTS idx_bill_documents_bill ON bill_documents (bill_id, document_type);
CREATE VIRTUAL TABLE IF NOT EXISTS bill_documents_fts USING fts5(
    title, text, content='bill_documents', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS bill_documents_ai AFTER INSERT ON bill_documents BEGIN
    INSERT INTO bill_documents_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS bill_documents_ad AFTER DELETE ON bill_documents BEGIN
    INSERT INTO bill_documents_fts(bill_documents_fts, rowid, title, text)
    VALUES ('delete', old.rowid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS bill_documents_au AFTER UPDATE ON bill_documents BEGIN
    INSERT INTO bill_documents_fts(bill_documents_fts, rowid, title, text)
    VALUES ('delete', old.rowid, old.title, old.text);
    INSERT INTO bill_documents_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
    name, proposer, committee, content='bills', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS bills_ai AFTER INSERT ON bills BEGIN
    INSERT INTO bills_fts(rowid, name, proposer, committee)
    VALUES (new.rowid, new.name, new.proposer, new.committee);
END;
CREATE TRIGGER IF NOT EXISTS bills_ad AFTER DELETE ON bills BEGIN
    INSERT INTO bills_fts(bills_fts, rowid, name, proposer, committee)
    VALUES ('delete', old.rowid, old.name, old.proposer, old.committee);
END;
CREATE TRIGGER IF NOT EXISTS bills_au AFTER UPDATE ON bills BEGIN
    INSERT INTO bills_fts(bills_fts, rowid, name, proposer, committee)
    VALUES ('delete', old.rowid, old.name, old.proposer, old.committee);
    INSERT INTO bills_fts(rowid, name, proposer, committee)
    VALUES (new.rowid, new.name, new.proposer, new.committee);
END;

CREATE TABLE IF NOT EXISTS speech_bill_links (
    speech_id TEXT NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
    bill_id TEXT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('EXPLICIT_MENTION','AGENDA_MATCH','SEMANTIC_RELEVANCE')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT,
    PRIMARY KEY (speech_id, bill_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_speech_bill_links_bill ON speech_bill_links (bill_id, confidence);

CREATE TABLE IF NOT EXISTS speech_relations (
    source_speech_id TEXT NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
    target_speech_id TEXT NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('QUESTION_TO','ANSWER_TO','FOLLOW_UP_TO','CONTINUES')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    PRIMARY KEY (source_speech_id, target_speech_id, relation_type)
);

CREATE TABLE IF NOT EXISTS embeddings (
    speech_id TEXT NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    vector_location TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (speech_id, model_name)
);

CREATE VIRTUAL TABLE IF NOT EXISTS speeches_fts USING fts5(
    text, speaker_name, speaker_role, organization, agenda,
    content='speeches', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS speeches_ai AFTER INSERT ON speeches BEGIN
    INSERT INTO speeches_fts(rowid, text, speaker_name, speaker_role, organization, agenda)
    VALUES (new.rowid, new.text, new.speaker_name, new.speaker_role, new.organization, new.agenda);
END;
CREATE TRIGGER IF NOT EXISTS speeches_ad AFTER DELETE ON speeches BEGIN
    INSERT INTO speeches_fts(speeches_fts, rowid, text, speaker_name, speaker_role, organization, agenda)
    VALUES ('delete', old.rowid, old.text, old.speaker_name, old.speaker_role, old.organization, old.agenda);
END;
CREATE TRIGGER IF NOT EXISTS speeches_au AFTER UPDATE ON speeches BEGIN
    INSERT INTO speeches_fts(speeches_fts, rowid, text, speaker_name, speaker_role, organization, agenda)
    VALUES ('delete', old.rowid, old.text, old.speaker_name, old.speaker_role, old.organization, old.agenda);
    INSERT INTO speeches_fts(rowid, text, speaker_name, speaker_role, organization, agenda)
    VALUES (new.rowid, new.text, new.speaker_name, new.speaker_role, new.organization, new.agenda);
END;
