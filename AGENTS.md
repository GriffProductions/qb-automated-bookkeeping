# AGENTS.md - Project Context & Architecture

## System Overview
This project is an automated financial ingestion engine that processes emails and PDFs across 28 company entities (Real Estate and Dialysis divisions), generates standardized file names by analyzing document content, structures metadata into JSON payloads, and prepares `qbXML` imports for QuickBooks Desktop.

## Directory Paths
- Real Estate Root (Dropbox): `C:\Users\user\Dropbox\Company Documentation & Financial Backups — Real Estate`
- Dialysis Root (Google Drive): `G:\My Drive\Abdeen & Griffith\Company Documentation & Financial Backups — Dialysis`

## Target Workflow
1. Ingest email text and PDF attachments.
2. Index historical subfolder PDFs to infer user naming conventions (date logic, vendor abbreviations, unit tags, memo phrasing).
3. Extract metadata using Vertex AI / Gemini (Structured Output via Pydantic).
4. Stage renamed PDFs in target company subfolders.
5. Generate validated `qbXML` payloads for Checks, Bills, and Class/Unit assignments.