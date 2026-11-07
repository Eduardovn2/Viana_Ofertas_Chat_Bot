# Viana_Ofertas_Chat_Bot — Fase 2.1

## Objetivo

Evoluir a Fase 2, sem refazer nem quebrar seus componentes, em duas frentes: **(A) categorização inteligente de produtos e filtragem por destino** e **(B) validação real de Docker, PostgreSQL e migrações Alembic**.

**Estado de partida informado pelo desenvolvedor:** Fase 2 concluída; agendador configurável, comparação Decimal, pontuação de ofertas, fila simulada, deduplicação, republicação após 3 horas, API administrativa e documentação. 173 testes passaram, um foi ignorado. Ruff, formatação e migrações em SQLite passaram. Ainda falta validar Docker Compose e PostgreSQL real.

## Orientações obrigatórias ao Codex

1. Inspecione primeiro `README.md`, `ROADMAP.md`, modelos, migrações, serviços, API e testes atuais. Reaproveite interfaces e convenções existentes; não recrie funções já implementadas.
2. Faça commits pequenos e coerentes, se o repositório já estiver configurado, sem sobrescrever alterações locais do usuário. Não force `git reset`, exclusão de volumes ou alterações destrutivas.
3. Priorize implementação, testes e documentação, com respostas curtas. Execute tarefas rotineiras sem pedir aprovação; solicite intervenção apenas para privilégios de administrador, credenciais, alterações destrutivas, despesas ou bloqueios reais.
4. **Não ative consultas reais às lojas nem envios ao Telegram/WhatsApp nesta fase.** Conserve o provedor simulado e a fila sem publicação efetiva.
5. Não acrescente serviços de IA pagos, scraping proibido nem dependências externas desnecessárias.
6. Proteja segredos (`.env` fora do Git). Preserve migrações já aplicadas e dados existentes.

## Parte A — Categorias inteligentes

### A1. Modelo de domínio e persistência

Criar ou adaptar, conforme o esquema existente:
- `Category`: id, slug estável, nome, descrição opcional, ativo, timestamps; unicidade do slug no escopo atual.
- `ProductCategory`: relacionamento N:N entre produtos e categorias, com origem (`store`, `rule`, `manual`), confiança opcional, timestamps, sem duplicações.
- `DestinationCategory`: relacionamento N:N entre destinos e categorias, com política de permitir/bloquear se pertinente à arquitetura existente.
- Preferências por destino: modo `all` (todas), `selected` (apenas selecionadas) e, se fizer sentido, exclusões explícitas. Definir semântica inequívoca para destino ainda não configurado; adotar padrão conservador (não publicar até configurado) e documentar.

Preparar extensão futura para multiusuário, **sem implementar login multiusuário, assinaturas ou cobrança** agora. Não incluir tenant fictício obrigatório que inviabilize o uso interno.

### A2. Categorias iniciais

Criar catálogo inicial idempotente (seed executável, migração de dados segura ou outro método compatível com o projeto):
- Casa e Decoração
- Materiais e Ferramentas
- Eletrodomésticos
- Moda
- Infantil
- Tecnologia
- Games
- Beleza
- Gerais (categoria de fallback para produtos não classificados, **não** um atalho implícito para enviar tudo)

**Destino desejado:** `Ofertas de Casa`. Categorias permitidas: Casa e Decoração, Materiais e Ferramentas, Eletrodomésticos, Moda, Infantil, Tecnologia, Games e Beleza. Caso o destino ainda não exista ou dependa de ID externo, permitir cadastrar um destino local **desativado**, aguardando configuração do canal/grupo real. Não fabricar IDs do Telegram ou WhatsApp. Não sobrescrever preferências existentes alteradas pelo administrador. A categoria `Gerais` não está selecionada inicialmente nesse destino.

### A3. Classificação automática sem custo de IA

- Prioridade: **correção manual > categoria confiável fornecida pela loja > regras locais de palavras-chave > fallback Gerais**.
- Suportar múltiplas categorias por produto, sem multiplicar publicações no mesmo destino.
- Usar nome, descrição e metadados já disponíveis; não inventar categoria baseada em informações ausentes.
- Normalizar acentos e maiúsculas/minúsculas e evitar falsos positivos óbvios; manter regras configuráveis e testáveis.
- Registrar origem e, quando aplicável, confiança da classificação.
- Não substituir manualmente categorias escolhidas pelo administrador durante reclassificação automática.
- Permitir reclassificação administrativa e explicação breve da regra aplicada.
- Definir como lidar com produtos ambíguos e campos vazios sem falhar a execução da fila.

### A4. Filtragem por destino

- Aplicar o filtro de categoria na **seleção para a fila por destino**, antes de qualquer envio futuro.
- Um produto com duas categorias aceitas gera no máximo uma entrada/publicação por destino e respeita a deduplicação e a janela de 3 horas da Fase 2.
- Categorias desativadas não devem qualificar novos envios.
- Destinos com modo `all` aceitam categorias sem necessidade de cadastrar cada uma; destinos com modo `selected` aceitam somente as categorias configuradas.
- Mudança de categoria ou regras de destino não pode ignorar restrições de repetição já existentes.
- Se ainda não existe mecanismo efetivo de roteamento por destino, implemente o contrato e os testes de seleção em modo simulado, sem inventar integrações de publicação.

### A5. API administrativa

Proteger as rotas com o mecanismo administrativo existente. Fornecer CRUD/ativação de categorias, associação e correção de categorias dos produtos, configuração das categorias de cada destino e endpoint/serviço para reclassificação. Usar validação, paginação quando aplicável e respostas HTTP coerentes. Não remover nem alterar contratos antigos sem necessidade.

## Parte B — Docker, PostgreSQL e Alembic reais

### B1. Diagnóstico do ambiente

Após reinicialização do computador e inicialização do Docker Desktop pelo usuário, verificar:

```powershell
docker --version
docker compose version
docker info
```

Se faltar `Virtual Machine Platform`, WSL 2, virtualização de BIOS ou permissão administrativa, **pare apenas essa frente**, descreva o bloqueio com comando/erro e continue os trabalhos independentes. Não tente contornar políticas corporativas nem alterar recursos do Windows sem autorização.

### B2. Inicialização segura

- Conferir `docker-compose.yml`, `.env.example` e valores reais do `.env` sem imprimir segredos.
- Não substituir `.env` existente e não recriar volumes sem autorização.
- Iniciar apenas o PostgreSQL com `docker compose up -d postgres` e confirmar `healthy` via `docker compose ps`.
- Aplicar `python -m alembic upgrade head` no banco de desenvolvimento, depois conferir `python -m alembic current`.
- Confirmar a API `/health` com `database: connected`.
- Se necessário criar migrações novas para categorias, seguir histórico Alembic existente e verificar upgrade; não utilizar `create_all` como substituto de migrações.

### B3. Teste de integração PostgreSQL

- Criar, somente se inexistente, banco descartável de testes **separado** do banco principal, preferencialmente `ofertas_bot_test`.
- Configurar `TEST_DATABASE_URL` de forma temporária, sem gravar senha em logs ou repositório.
- Rodar a suíte PostgreSQL existente e novos testes de categorias nesse banco.
- Não executar testes destrutivos contra `ofertas_bot` de desenvolvimento.
- Conferir restrições, unicidades, chaves estrangeiras e comportamento das migrações no PostgreSQL real.
- Preservar volume e dados ao parar o Compose; evitar `docker compose down -v`.

## Testes e critérios de aceite

- Suíte anterior da Fase 2 permanece verde (baseline informado: 173 aprovados e 1 ignorado; explicar divergências legítimas).
- Ruff e formatação aprovados.
- Alembic cria/atualiza esquema no PostgreSQL sem apagar dados.
- Seed de categorias pode rodar duas vezes sem duplicar dados.
- Categoria manual permanece após nova classificação automática.
- Produto de várias categorias gera apenas uma entrada de fila por destino.
- `Ofertas de Casa` recebe apenas as oito categorias escolhidas; `Gerais` não é implicitamente liberada.
- Produtos sem classificação, categorias desativadas e destinos sem configuração seguem política documentada.
- Deduplicação e republicação de três horas continuam funcionando.
- Nenhuma publicação real ou requisição não autorizada às lojas ocorre.
- Documentação e `ROADMAP.md` atualizados com estado efetivamente verificado e pendências.

## Ordem de execução recomendada

1. Inspecionar o repositório, estado Git e testes atuais.
2. Diagnosticar Docker. Se estiver acessível, subir PostgreSQL e validar migrações da Fase 2.
3. Implementar modelos, migrações, seed e API de categorias.
4. Implementar classificador local e regras de roteamento por destino.
5. Validar integração com PostgreSQL, executar suíte completa e corrigir falhas.
6. Atualizar documentação e apresentar resumo final breve.

## Relatório final do Codex

Responder objetivamente com:
1. Itens implementados e arquivos principais alterados.
2. Testes (quantidade aprovados/falhos/ignorados) e resultado Docker/PostgreSQL/Alembic.
3. Bloqueios remanescentes, sem fingir validação que não ocorreu.
4. Próximo passo para Fase 3 (fontes reais autorizadas).

**Esta Fase 2.1 está autorizada integralmente. Não avançar para a Fase 3 sem minha confirmação.**
