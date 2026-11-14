# Revisão integral do código — 13/11/2026

Escopo lido: `transpiler/`, `runtime/`, `security/`, `services/`, `infra/init.sql`, `assets/`, Dockerfiles, Compose, spec JSON, testes e documentação. Duas revisões independentes (conformidade com a especificação e qualidade/segurança), seguidas de verificação independente das evidências finais. Nenhum defeito bloqueante conhecido permanece na demonstração sintética.

## Achados corrigidos e validados

- O `State` real do ADK não implementa `Mapping`: callbacks agora inspecionam `to_dict()`. Assinaturas opacas `thought_signature` não são PII; outros bytes continuam bloqueados (`security/adk_callbacks.py`, testes de ambos os lados).
- O NER confundia nomes de exames/categorias com pessoas no OCR e na API: léxico compartilhado permite termos do catálogo, rejeitando nomes de pessoas nos testes (`security/pii_guardrail.py`, `services/scheduling_api/schemas.py`). A regex CNS não deve casar a parte decimal de `confidence_score`.
- O agente gerado tolera cercas Markdown e o wrapper OpenAPI `<operação>_response`, preservando validação estrita contra a resposta efetiva. O template injeta origem absoluta no spec OpenAPI antes do POST. O drift-guard compara o artefato gerado à spec/template e detectou uma divergência durante o desenvolvimento (`transpiler/templates/`, `tests/unit/test_generated_drift.py`).
- Protocolo sem sequência longa de dígitos evita falso positivo de RG/telefone/CNS (`services/scheduling_api/main.py`, teste de 100 IDs). Limites de 50 termos e 200 caracteres/termo protegem a fronteira MCP RAG. O POST compara estrutura e strings exatas, tolerando apenas arredondamento mínimo de scores de ponto flutuante.
- `make transpile --no-deps`, exclusões do contexto Docker, variáveis mortas removidas do `.env.example`/agente CLI e contrato SQL alinhado. Nenhum segredo foi encontrado nos artefatos `evidence/` (busca por padrões de chave/senha).

## Controles conferidos

SQL parametrizado; caminho OCR restrito ao volume por `resolve`, `O_NOFOLLOW` e `S_ISREG`; validação de schema e imports no transpiler; callbacks impedem POST sem RAG verificado; API exige detector local e transação atômica; catálogo exclusivamente `MOCK-*`. Revisores não encontraram bypass demonstrado desses controles.

## Riscos/limitações aceitos (não corrigidos nesta etapa)

- A suíte automatizada usa `FakeRunner` e cursor SQL fake; não demonstra migrações nem protege entradas clínicas arbitrárias. O smoke real e uma execução sintética complementam a suíte, sem substituir testes de produção.
- O léxico anti-falso-positivo só conhece catálogo/categorias; exames fora dele podem ser bloqueados. Regexes brasileiras podem rejeitar texto legítimo com números parecidos com telefone. Fail-closed reduz vazamento, mas não garante anonimização de qualquer PII.
- `init.sql` só roda com volume PostgreSQL novo; sem migração automática. `Dockerfile.runtime` instala extras dev para permitir testes na imagem; gerador da imagem usa fontes DejaVu Linux.
- Uma falha **após o POST** pode registrar a solicitação sem confirmação da CLI; conferir o protocolo/GET antes de tentar novamente. Não há idempotency key para a criação. O banco contém registros de tentativas diagnósticas anteriores à execução final; não interpretar contagem total de registros como uma única execução.
- OCR do adendo é fonte itálica simulada, não manuscrito real; não há métricas de latência, acurácia clínica ou garantia para dados reais.

## Verificação observada

`evidence/06-tests.txt`: 68 testes mock-only, 88% de cobertura, Ruff limpo e Compose config OK. `evidence/05-run-full.txt`: exit 0 e seis exames `MOCK-*` em `CONFIRMED`; `evidence/05-protocol-get.json`: mesmo protocolo e seis itens. O revisor independente identificou uma afirmação excessiva na tabela do README sobre a captura de codegen; a tabela foi corrigida. A revisão de mudanças ocorreu em etapas durante a implementação, mas não houve uma segunda auditoria independente de todo o intervalo de commits; o repositório Git criado por engano foi removido conforme instrução do usuário.
