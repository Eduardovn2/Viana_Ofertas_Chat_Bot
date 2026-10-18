# Roadmap do Ofertas Bot

Esta entrega cobre a fundação do monólito modular. Cada fase seguinte depende de aprovação explícita do responsável pelo projeto. Os modelos reservados no banco não significam que a funcionalidade correspondente já existe.

## Fase 1 — Fundação

Concluída em **18/10/2026**, com 88 testes aprovados, 1 teste PostgreSQL pulado, validação em SQLite e compilação da migração para PostgreSQL. O Docker não estava disponível no `PATH` do ambiente de execução; o Compose e a integração com PostgreSQL real não foram executados. O teste PostgreSQL está preparado para execução opcional com `TEST_DATABASE_URL` em banco exclusivo com sufixo `_test`.

- [x] Estrutura Python 3.12+, FastAPI, SQLAlchemy e configurações por ambiente.
- [x] PostgreSQL local com Docker Compose e migração Alembic versionada.
- [x] Modelos iniciais para produtos, histórico, links, cupons, ofertas, destinos, publicações, monitoramento e configurações.
- [x] CRUD administrativo de produtos e links de afiliado manuais.
- [x] Ativação/desativação do monitoramento por produto.
- [x] Consulta manual ao provedor simulado e armazenamento do histórico.
- [x] API administrativa protegida por chave; servidor local por padrão.
- [x] Testes automatizados e instruções Windows/PowerShell.

O preço simulado não é uma oferta real. Nenhuma consulta comercial, descoberta automática, rotina agendada ou publicação é executada nesta fase.

## Fase 2 — Monitoramento e regras de preços

**Aguardando aprovação para iniciar.**

- Formalizar atualização, validade e procedência dos dados de preço.
- Diferenciar preço anterior da loja de queda comprovada no histórico.
- Tratar preço público, restrições de assinatura/VIP, disponibilidade, frete e parcelamento.
- Adicionar o agendador e intervalos configuráveis, condicionados às permissões de cada fonte.
- Implementar limites de requisições, timeout, backoff e prevenção de execuções concorrentes repetidas.
- Testar dados ausentes, preços inválidos, informação desatualizada e comparação histórica.

**Critério de avanço:** comportamento demonstrado com dados simulados e aprovação para integrar fontes reais.

## Fase 3 — Fontes comerciais autorizadas

- Confirmar credenciais, documentação oficial, permissões e limites para cada operação.
- Implementar conectores independentes de Mercado Livre e Shopee apenas para operações autorizadas.
- Manter conectores sem credenciais desativados sem prejudicar o restante do sistema.
- Registrar origem e horário de cada informação e explicitar limitações das fontes.
- Manter o link do Mercado Livre como cadastro manual pelo Link Builder oficial, salvo autorização documentada de outra operação.
- Testar falhas, autenticação, dados incompletos e rate limiting com respostas controladas.

**Critério de avanço:** consulta demonstrada com acesso autorizado; nenhum endpoint privado ou contorno de proteção.

## Fase 4 — Publicação Telegram

- Implementar `MessageTemplateService`, omitindo campos ausentes e usando identificação publicitária aplicável.
- Implementar Telegram pela API oficial com token no ambiente, destinos e modo de teste.
- Bloquear publicação sem link de afiliado próprio, preço válido ou informação atualizada.
- Adicionar aprovação manual, registro de envio, erros e limites por destino.
- Publicar mensagens ou imagens somente após configuração e autorização do responsável.

**Critério de avanço:** conteúdo revisado no modo de teste e aprovação explícita antes do primeiro envio real.

## Fase 5 — Cupons, prioridades e fila

- Implementar `CouponService` e os estados `CONFIRMED`, `UNVERIFIED` e `INVALID`.
- Calcular preço final apenas quando as regras conhecidas e a aplicabilidade permitirem.
- Implementar `OfferScoringService` configurável; cupom permanece opcional.
- Implementar fila, tentativas limitadas e deduplicação por plataforma, produto, destino e histórico.
- Aplicar janela de três horas e revalidar preço/cupom antes de republicar.
- Cobrir regras de cupom, pontuação, duplicação e templates com testes próprios.

**Critério de avanço:** nenhuma promessa de desconto sem verificação; ausência de cupom não impede oferta válida.

## Fase 6 — Operação contínua

- Definir implantação, HTTPS, acesso administrativo, backups e recuperação.
- Monitorar falhas operacionais e adicionar observabilidade sem expor segredos.
- Validar migrações e concorrência com PostgreSQL em integração contínua.
- Definir retenção do histórico e procedimentos de manutenção.
- Avaliar WhatsApp somente após validar uma solução autorizada para o destino pretendido.

## Fora do MVP pessoal

SaaS multiusuário, cadastro comercial de clientes, planos, cobrança, pagamentos e painel comercial não fazem parte das fases acima. Exigem escopo e aprovação próprios.
