# 03 Multi Model Architecture

Source file: 03_Multi_Model_Architecture.pptx


## Slide 1

Multi-Model Architecture

Ollama is the head · Models are specialists

ZEUS · Local AI · Confidential

## Slide 2

Why multi-model?

•  One model cannot be best at coding, vision, and math
•  Small models are faster for routing and simple commands
•  7B models used only when the task needs them
•  Saves RAM and improves response quality

## Slide 3

Model Roles

Workers

• 0.5B / 3B → fast chat & routing
• Coder 7B → Python & debug
• R1 7B → hard reasoning
• Moondream / VL → images

Support

• BGE-M3 → embeddings
• BGE-Reranker → evidence rank
• Aya → multilingual
• Mistral / Phi → general fallback

## Slide 4

Request Pipeline

•  User request → Intent detection
•  Context + memory retrieval (only relevant bits)
•  Task classification → Model router
•  Specialist agent + tools
•  Validation → Auto-fix if needed → Final answer

## Slide 5

Resource Rules (16 GB RAM)

•  Prefer smallest capable model
•  Avoid loading two 7B models at once
•  Use tools for math, files, and Office — not the LLM
•  Release inactive models when possible

## Slide 6

Summary

Right model · Right tool · Right time

ZEUS · Local AI · Confidential