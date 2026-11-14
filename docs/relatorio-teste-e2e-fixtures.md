# Relatório histórico de teste E2E — pedidos médicos fictícios

> **Nota de versão:** esta bateria ocorreu **antes** da decisão de aceitar somente pedidos digitados completos com todos os exames reconciliados. Os registros `PARTIAL` e as imagens cortadas aceitas abaixo descrevem a versão anterior, não a política atual. Não alteramos os logs nem os resultados históricos. A política atual ainda não foi revalidada com Gemini.

## Objetivo e escopo

Avaliar como o pipeline **imagem → OCR e anonimização → extração de exames pelo agente → busca híbrida no catálogo fictício → registro pela API → confirmação pela CLI** se comporta com imagens legíveis, desfocadas, sombreadas e cortadas. A avaliação procura tanto acertos (exames preservados, PII mascarada, rejeição de imagem ilegível) quanto falhas perigosas (registro incompleto, falsa confirmação ou registro sem confirmação da CLI).

Foram inspecionadas visualmente e executadas **31 imagens** de `assets/`: a imagem principal, seis fixtures legadas e quatro variantes de cada uma das seis solicitações fictícias. O catálogo de expectativas foi salvo **antes** das execuções em [`evidence/fixture-audit/catalog.json`](../evidence/fixture-audit/catalog.json). Houve **uma execução autorizada da CLI com Gemini por imagem**, sem tentativas repetidas. As imagens `agent-cli`, `mcp-ocr` e `scheduling-api` foram reconstruídas antes da bateria; seus identificadores estão em [`images.txt`](../evidence/fixture-audit/images.txt). RAG e Postgres estavam healthy. Os resultados são uma amostra dessas versões, **não uma garantia de comportamento determinístico ou segurança clínica**.

Artefatos para auditoria: [catálogo individual](../evidence/fixture-audit/catalog.json), [relatório tabular](../evidence/fixture-audit/report.md), [sequência e duração](../evidence/fixture-audit/progress.txt) e [saída da CLI por imagem](../evidence/fixture-audit/logs/). O catálogo registra expectativa visual, exit, status, códigos `MOCK-*`, termos sem correspondência, protocolo, resultado da consulta GET quando aplicável e variação da contagem de registros no banco. O catálogo e os logs foram examinados por padrões de chave, senha, CPF e e-mail não mascarados; **nenhum padrão foi encontrado**. As imagens contêm dados inventados e não devem ser usadas como teste de privacidade para pessoas reais.

## Critérios definidos pela inspeção visual

| Variante | O que está visível | Comportamento desejado |
|---|---|---|
| Boa | Identificação fictícia, quatro linhas numeradas, adendo itálico, médico e rodapé legíveis. | Mascarar identificação; extrair somente os exames visíveis; registrar `CONFIRMED` se todos os termos extraídos forem encontrados, ou `PARTIAL` com todos os não encontrados discriminados. |
| Ruim | Desfoque forte impede leitura confiável inclusive da identificação. | Bloquear sem registro; não inferir dados ilegíveis. |
| Sombra | Parte da identificação e dos exames recebe sombra escura, mas há texto discernível. | Prosseguir somente se OCR/anonimização forem confiáveis; não inventar itens. |
| Cortada | Identificação e quatro linhas numeradas permanecem; adendo itálico e assinatura/rodapé foram removidos. | Idealmente indicar documento incompleto antes de apresentar a solicitação como íntegra. **O produto atual não possui verificador de integridade documental**: o teste distingue o que seria desejável do que a implementação pode fazer. |

As seis solicitações diferem nos exames: Carlos (hemograma/glicemia/TSH/lipidograma/vitamina D/ferritina), Ana (urina I/ureia/creatinina/ácido úrico/sódio/potássio), Rafael (TGO/TGP/Gama GT/bilirrubinas/fosfatase), Juliana (PCR/VHS/fator reumatoide/FAN/complementos), Pedro (PSA/testosterona/LH) e Marina (citologia/cultura/bacterioscopia/pesquisas microbiológicas). O nome do arquivo (`invalid_*`, `good_*`) é apenas rótulo da fixture, **não uma regra implementada**.

## Resultado medido

| Resultado da CLI | Quantidade | Registro criado durante a execução |
|---|---:|---|
| Exit 0 — `CONFIRMED` | 3 | Sim |
| Exit 0 — `PARTIAL` | 10 | Sim |
| Exit 50 — guardrail | 11 | Não detectado na janela de cada execução |
| Exit 40 — pipeline/confirmação | 7 | **Um** caso criou registro; seis não aumentaram a contagem no intervalo observado |
| **Total** | **31** | **14** novos registros na bateria |

Entre as sete imagens com desfoque forte (`__bad` mais `invalid_blur_heavy.jpg`), **nenhuma recebeu exit 0**: seis saíram com 50 e `marina__bad.jpg` com 40, todos sem novo registro detectado. As três confirmações totais vieram do pedido principal e das duas versões nítidas de Carlos (`good_clean.png` e `carlos__good.png`), cada uma com seis códigos fictícios. Não interpretar `CONFIRMED` como validação clínica do conteúdo original: significa somente que os **termos extraídos** foram registrados.

## Acertos observados

1. **Rejeição de desfoque severo:** nenhum dos sete casos fortemente borrados foi registrado. O `mcp-ocr` exige confiança média ≥ 0,8 e analisador local de PII; falhas viram `GuardrailViolation` (`services/mcp_ocr/server.py`, `ocr_engine.py`). O exit isolado não prova qual condição interna falhou.
2. **Registro auditável dos itens encontrados:** em 13 execuções com exit 0, a CLI apresentou protocolo e lista de códigos `MOCK-*`, e as consultas GET dos protocolos confirmaram o estado registrado (`catalog.json`). Itens sem correspondência apareceram como `unmatched_terms` nos dez registros `PARTIAL`.
3. **Preservação do adendo na imagem nítida de Carlos:** as duas versões nítidas e a imagem principal registraram seis itens, incluindo vitamina D e ferritina do adendo itálico. A sombra em `carlos__shadow.jpg` levou a cinco itens registrados e ferritina sem correspondência; não houve falsa atribuição de código à ferritina naquele resultado.
4. **Bloqueio sem exposição nos logs da CLI:** os logs de erro fornecem exit e etapa aproximada, sem imprimir texto bruto do OCR ou identificadores das imagens. A ausência de padrões sensíveis nos arquivos examinados não mede a eficácia do guardrail para entradas arbitrárias.

## Erros e problemas encontrados

### 1. Registro efetuado apesar do exit 40 — prioridade alta

`fixtures/good_rotated.jpg` terminou com **exit 40** após iniciar a etapa de registro, porém a contagem do banco aumentou em um. Foi recuperado o protocolo `LAB-2026-11-13-OWCMUOCLAE`; o GET confirmou `PARTIAL`, com três itens (`MOCK-0123`, `MOCK-0014`, `MOCK-0015`) e `Acido Urico`/`Sodio e Potassio` sem correspondência. **Não houve retry.**

**Cadeia causal conhecida:** o POST pode persistir (`services/scheduling_api/main.py:create_exam_appointment`) antes de o agente gerar sua saída final; `after_agent_callback` compara `booking_result` com `_booking_verified` (`transpiler/templates/adk_sequential_agent.py.j2:_validate_output`), e `runtime/cli.py:validate_confirmation` faz a validação final. Uma falha depois do POST deixa o registro no banco, embora a CLI retenha a confirmação. **Hipótese ainda não comprovada para este caso:** divergência/erro de formatação da resposta final estruturada (`output_schema`/`set_model_response`), falha na comparação de evidência ou exceção do ADK após a API. O log público genérico não contém a exceção exata nem a resposta interna; não é possível escolher entre essas causas apenas com o material coletado.

**Impacto:** o usuário pode repetir uma operação que já registrou solicitação, criando duplicata. A API não recebe chave de idempotência e a CLI não apresenta protocolo na falha. Antes de reexecutar, localizar o registro por meio operacional seguro. A diferença de contagem e o registro mais recente foram correlacionados com a janela da execução; isso não equivale a rastreamento transacional da requisição.

### 2. Registro de imagem cortada — prioridade alta para interpretação

Quatro fixtures `__cropped.jpg` terminaram com exit 0 e `PARTIAL`: Ana (3 itens), Carlos (3), Juliana (3) e Rafael (3). Visualmente o adendo e a assinatura **não estão presentes**; `invalid_cropped.jpg` e `marina__cropped.jpg` saíram com 40 sem novo registro, e `pedro__cropped.jpg` com 50 sem novo registro. Logo, o nome `invalid_cropped` não impõe rejeição uniforme.

**Causa de produto identificável no código:** `check_image` (`runtime/cli.py`) e `extract_text` (`services/mcp_ocr/ocr_engine.py`) validam caminho, extensão, cabeçalho, decodificação, tamanho e OCR, mas **não identificam borda cortada, ausência de assinatura ou número total de linhas esperado**. A busca marca como `unmatched_terms` somente termos extraídos que não atingem o limiar (`services/mcp_rag/hybrid_search.py`). O adendo ausente não chega à busca; portanto `PARTIAL` não informa a perda desse trecho. Não é correto atribuir a perda a um bug de RAG: a informação não está na imagem cortada.

### 3. Imagens visualmente boas bloqueadas — prioridade média

`juliana__good.png`, `marina__good.png` e `rafael__good.png` receberam **exit 50** logo após a validação de imagem/serviços, sem registro novo detectado. Os arquivos são visualmente legíveis nas pranchas. `marina__shadow.jpg` também saiu com 50; outros casos do mesmo pedido variaram entre 40/50.

**Hipóteses, não diagnósticos:** confiança média do OCR < 0,8 apesar da legibilidade humana; falha/ruído na identificação de CPF/RG/CRM; falso positivo do NER que classifica nome de exame como pessoa apesar do léxico; PII residual detectada na segunda verificação; exceção local mascarada como `GuardrailViolation`. `services/mcp_ocr/server.py` captura qualquer exceção e a converte em bloqueio de privacidade, deliberadamente sem revelar texto sensível; `security/pii_guardrail.py` usa regex, Presidio e proteção lexical do catálogo. O exit 50 indica fail-closed, **não comprova qual hipótese ocorreu**. Diagnóstico futuro deveria usar telemetria interna mínima (etapa e tipo de erro, sem OCR bruto/PII) ou probes isolados autorizados, não enfraquecer o guardrail para obter exit 0.

### 4. Exit 40 sem registro e variabilidade RAG/OCR — prioridade média

`degraded_blur_light.jpg`, `degraded_shadow.jpg` e `juliana__shadow.jpg` chegaram ao início do registro, mas saíram com 40 **sem aumento do banco na janela medida**. `invalid_cropped.jpg` e `marina__cropped.jpg` chegaram apenas até a conclusão da extração; `marina__bad.jpg` também saiu com 40. Os logs não mostram argumento/retorno das tools nem exceção interna, portanto não há evidência para apontar um único módulo como causa de cada falha.

Exemplos de extração/reconciliação parcial: `ana__good.png` registrou urina I, ureia e creatinina, mas deixou `Acido Urico` e `Sodio e Potassio` sem match; `pedro__good.png` registrou quatro itens, com LH sem match; `rafael__shadow.jpg` deixou `BilirrubinasTotais e Fracoes` sem match. A busca usa **limiar 0,80** (`services/mcp_rag/hybrid_search.py:THRESHOLD`); grafia OCR degradada, separação de palavras, normalização, sinônimos do catálogo e o limiar são **hipóteses plausíveis**, mas o catálogo/score exato de cada termo nessa bateria não foi capturado. Evitar afirmar que o LLM ou o RAG isoladamente errou sem reproduzir a etapa.

### 5. Limitações de diagnóstico e teste

- `services/mcp_ocr/server.py` e `security/pii_guardrail.py` convertem falhas diversas em exceção genérica de privacidade; a CLI (`runtime/cli.py`) apresenta mensagem segura, mas sem etapa interna/causa técnica. Essa decisão protege contra vazamento e dificulta diferenciar imagem ruim, falso positivo de PII e falha de infraestrutura.
- A interface mostra progresso pela autoria dos eventos ADK; **iniciar** uma etapa não prova sucesso dela. A confirmação só deve ser apresentada após verificar estado final, mas pode já existir POST persistido antes da falha.
- As imagens de adendo usam fonte itálica simulada, não caligrafia real; os pedidos são fictícios, e o catálogo contém apenas `MOCK-*`. O teste não mede acurácia clínica, recall de PII ou resistência a imagens adversariais.
- Existem variantes legadas e variantes novas com conteúdo visual relacionado; 31 execuções não equivalem a 31 solicitações independentes. O LLM pode variar entre execuções. Não foram feitos retries para forçar resultados favoráveis.

## Recomendações de investigação (sem alteração nesta tarefa)

1. **Antes de repetir uma falha 40**, verificar se houve POST e consultar o registro; definir estratégia de idempotência e correlação segura de tentativa/protocolo. Testar a falha *depois* do POST com banco simulado e confirmar que a interface não induz retry cego.
2. Instrumentar pontos internos de erro com **códigos de etapa e tipos de exceção**, nunca texto OCR, prompt, dados de pessoa, chave ou payload de ferramentas. Com isso, separar falhas em `extract_text`, `redact_pii`, callbacks, busca, API e validação final.
3. Investigar as três imagens boas bloqueadas com OCR local e detectores em ambiente controlado; medir confiança e tipo de bloqueio antes de mexer no limiar 0,8 ou no léxico anti-PII.
4. Decidir em produto se ausência de assinatura/adendo ou corte deve bloquear toda a solicitação, exigir confirmação humana ou apenas gerar alerta; não reutilizar `PARTIAL` do RAG para representar documento incompleto.
5. Para termos sem match, guardar **apenas** termos sintéticos e scores em ambiente de teste, comparar OCR bruto localmente sem exportar PII e avaliar sinônimos/limiar com conjunto etiquetado. Não reduzir o limiar por causa de um único exemplo.

## Reprodutibilidade e limites da evidência

Comando executado uma vez por arquivo, após catalogação visual (substituindo `<arquivo>` pelo caminho relativo a `assets/`):

```bash
docker compose run --rm agent-cli python -m runtime.cli run \
  --agent-module runtime.generated.clinical_exam_scheduler_agent \
  --image /app/inputs/<arquivo>
```

A contagem de `scheduling.appointments` antes/depois foi usada para identificar novos registros; quando a CLI retornou protocolo, o GET verificou status e itens. Em `good_rotated.jpg`, o protocolo foi recuperado pelo registro mais recente no intervalo e confirmado por GET. Não há dump de estado interno do ADK para atribuir sua exceção exata. Este relatório descreve **o que foi observado e hipóteses delimitadas**, não uma certificação de segurança para pedidos reais.
