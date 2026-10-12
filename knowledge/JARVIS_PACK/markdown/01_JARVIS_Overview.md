# 01 ZEUS Overview

Source file: 01_JARVIS_Overview.pptx


## Slide 1

ZEUS AI Desktop Assistant

Local · Offline-first · Multi-model Operating System

ZEUS · Local AI · Confidential

## Slide 2

What is ZEUS?

•  Personal AI desktop assistant for real computer work
•  Runs fully on your PC — core features need no cloud LLM
•  Uses Ollama local models for chat, coding, vision, reasoning
•  Controls apps, files, documents, voice, calendar, tasks, notes
•  Built for Windows with modest hardware (8–16 GB RAM)

## Slide 3

Core Capabilities

Intelligence

• Natural language chat
• Multi-model routing
• Memory & preferences
• RAG over documents
• Voice STT + TTS

Actions

• Open applications
• Type into any app
• Word / Excel / PPT files
• Screenshots & OCR
• System monitoring

## Slide 4

Architecture Snapshot

•  Orchestrator receives user intent (text or voice)
•  Task analyzer + planner decompose the request
•  Model router picks the smallest capable local model
•  Tools execute deterministic work (files, Office, automation)
•  Validators check results before delivery to the user

## Slide 5

Hardware Fit

•  Target: Intel i5 + 16 GB RAM + Intel Iris Xe
•  Prefer Q4 quantized 0.5B–7B models via Ollama
•  One heavy model active at a time to protect RAM
•  Deterministic tools used instead of LLM where possible

## Slide 6

Thank You

Questions · Demo · Next Steps

ZEUS · Local AI · Confidential