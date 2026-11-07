# VIANA OFERTAS — CORRIGIR ORDEM DO PILOTO: SAFE PRIMEIRO

O responsável definiu expressamente a sequência:

1. Primeiro, piloto SAFE com uma única leitura.
2. Depois, se o SAFE for satisfatório, piloto separado de 6 horas.

O plano recorrente já preparado:
UUID 1cfbb654-f4ab-4146-b185-e9f48e702540
Hash 9492714e8d0bfe038574a63904bbf647bcecf3fe2aa8694f9b57b71056b5b24b

Esse plano prevê duas leituras e NÃO deve ser aprovado ou executado neste momento.

## OBJETIVO

Preparar um plano SAFE independente usando o fluxo ShopeeSingleReadAdapter / identity-reads já existente.

Produto:
- product_id: 2
- shopId: 1904573791
- itemId: 23199869203

Escopo SAFE:
- Exatamente uma tentativa máxima de POST.
- productOfferV2, page=1, limit=1.
- Uma conta vinculada por fingerprint.
- Sem retry, fallback ou paginação extra.
- Sem monitoramento recorrente.
- Sem ativação de scheduler.
- Sem mutation ou geração de links.
- Sem Telegram.
- Sem nova observação operacional nesta etapa de preparação.

## TAREFAS

1. Verificar o estado atual do plano recorrente anterior, sem aprová-lo nem consumi-lo.
2. Preparar um novo plano pontual SAFE, com UUID e hash próprios.
3. Usar o mecanismo de autorização durável e bloqueio de duplicações já implementado.
4. Conferir a identidade do produto, conta, URL e payload.
5. Verificar se há restrições oficiais conhecidas; documentar quotas desconhecidas.
6. Definir uma janela futura curta, sem assumir que a autorização já foi concedida.
7. Executar somente validações locais necessárias.
8. Informar o UUID, hash, validade, estado e procedimento administrativo de aprovação e execução.

Não executar HTTP real neste comando.
Não criar aprovação automática.
Não consumir autorização histórica.
Não alterar os históricos 2/3/4.
Não habilitar integrações ou scheduler.
Não fazer commit ou push.

Se o plano SAFE já puder ser produzido pelo fluxo existente sem alterar código, não criar código novo.

ENTREGAR somente o plano SAFE preparado e os bloqueios encontrados, aguardando autorização explícita antes da consulta real.