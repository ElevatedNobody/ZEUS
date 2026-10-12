# 02 Model Inventory RAM

Source file: 02_Model_Inventory_RAM.xlsx


## Sheet: Models

| Ollama Model Inventory & RAM Budget |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- |
| Model | Role | Size (GB approx) | Priority | Installed? | Notes |
| qwen2.5:0.5b | Fast / routing | 0.4 | Must | Check | Simple commands |
| qwen2.5:3b | General chat | 2 | Must | Check | Default chat |
| qwen2.5-coder:7b-instruct-q4_K_M | Coding | 4.7 | Must | Check | Python / debug |
| deepseek-r1:7b-qwen-distill-q4_K_M | Reasoning | 4.7 | Must | Check | Hard problems |
| moondream | Fast vision | 1 | Must | Check | Screenshots |
| bge-m3 | Embeddings | 1.2 | Must | Check | RAG |
| dengcao/bge-reranker-v2-m3 | Rerank | 1.2 | Must | Check | Evidence rank |
| qwen2.5vl:3b | Vision deep | 3 | Recommended | Check | Image analysis |
| mistral:7b-instruct | General fallback | 4.4 | Recommended | Check |  |
| phi3.5:3.8b | Fast general | 2.2 | Recommended | Check |  |
| hermes3:3b | Agent-style chat | 2 | Optional | Check |  |
| aya-expanse:8b | Multilingual | 5 | Optional | Check | Heavy on 16GB |
| Must-have size sum (GB): |  |  |  |  |  |
| System RAM (GB): | 16 |  |  |  |  |
| Note: | Load one 7B model at a time on 16 GB systems |  |  |  |  |