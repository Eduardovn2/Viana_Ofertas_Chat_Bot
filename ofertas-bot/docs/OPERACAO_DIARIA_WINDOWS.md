# Operação diária no Windows

O aplicativo usa o PostgreSQL existente, a API em `127.0.0.1:8000` e o processo `app.daily_worker`. O worker deriva um AUTO_PILOT por data de Brasília a partir da autorização recorrente assinada. Cada dia tem reservas e prazo próprios. O ranking, o publicador e a outbox continuam nos serviços existentes.

## Abrir o painel

No PowerShell, a partir da pasta do projeto:

```powershell
.\scripts\vianinha.ps1 health
.\scripts\vianinha.ps1 open
.\scripts\vianinha.ps1 login
```

Digite no painel o código de uso único impresso no terminal. Ele vence em cinco minutos. A sessão local usa cookie HttpOnly e a chave administrativa fica no servidor.

O painel mostra horários, orçamentos, bloqueios e histórico de publicações. Os indicadores Shopee e Telegram representam o último resultado registrado. Os filtros de histórico consultam os registros reais da outbox.

## Inicialização no Windows

`install` registra a tarefa **VianaOfertas - Operacao diaria** no Agendador de Tarefas, com disparos ao entrar no Windows e às 08h. `start` inicia a tarefa registrada. `run` pode ser usado em um terminal para iniciar a mesma API e o worker. `validate` confere os módulos do `.venv` e o fuso horário.

Se o PostgreSQL estiver desligado, o script inicia o contêiner já existente `ofertas-bot-postgres-1`. Uma instalação nativa pode informar `-PostgresService NOME_DO_SERVICO` ao instalar a tarefa. O processo usa o usuário atual e o perfil interativo do Windows. Para funcionar após reinício, entre nesse usuário. Suspensão ou falta de conexão podem reduzir a janela efetivamente trabalhada.

```powershell
.\scripts\vianinha.ps1 validate
.\scripts\vianinha.ps1 install
.\scripts\vianinha.ps1 start
```

A tarefa ignora um segundo disparo enquanto está em execução. O PostgreSQL serializa ciclos mesmo quando há um processo concorrente. Na retomada, o relógio e os registros do dia definem o próximo trabalho permitido. Consultas atrasadas não formam um lote de compensação.

## Autorização e controles

1. Prepare o escopo com dias, horário, destino, modo, termos e limites. Confira o hash, as operações e o orçamento exibidos no painel.
2. Confirme o escopo selecionado e use **Iniciar**. O worker espera a abertura da janela. Para o teste de um dia e até 30 minutos, o painel pede o reconhecimento explícito de quotas ainda não confirmadas. Uma autorização recorrente exige referência e valores documentados das quotas.
3. **Pausar** bloqueia novos POSTs e conserva o orçamento. **Retomar** libera o restante da janela original. **STOP** impede novas operações no dia e registra o encerramento. A autorização vigente pode voltar a operar no próximo dia permitido. Para manter a operação suspensa por vários dias, use **Pausar**.
4. Às 23h, ou no fim configurado, o prazo assinado bloqueia novas operações. O worker encerra o registro do dia, reconcilia reservas interrompidas e devolve o destino ao estado anterior. Uma tentativa incerta permanece bloqueada para repetição.

O comando `.\scripts\vianinha.ps1 stop` registra STOP mesmo sem o navegador. A API fica disponível para conferência do histórico. Um POST já iniciado pode concluir durante a pausa ou parada.

O modo `ANNOUNCEMENT_ONLY` usa PRO_V2. O worker considera uma cotação oficial recente, prioriza pelo ranking existente, gera link próprio se houver orçamento e prova de conta válida e revalida a mensagem antes do despacho. O modo `VERIFIED_OFFER` conserva o fluxo documental e suas exigências. Alterar limites, destino ou regras de operação cria outro escopo para confirmação.

Os escopos preparados em 06/11 estão em `AUTORIZACOES_DIARIAS_PREPARADAS_20261106.json`.

A proposta de produção de 100 tentativas por dia está em
`PROPOSTA_DIARIA_100_ce223e2e-d257-43a1-aee9-0852ded98507.json`. O painel
separa mensagens confirmadas de reservas consumidas. Ao esgotar os envios,
o ciclo conserva o horário e o orçamento de descoberta. A cadência por janela
e o teto horário do destino continuam em vigor. A proposta mantém consultas,
geração de links e intervalo de busca pendentes de quotas documentadas.

Para validar o painel, a retomada e a cadência com transportes simulados:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_daily_operation.py
```

## Política contínua DAILY_V3

Novos escopos criados no painel usam a política contínua. Os tetos administrativos
diário, horário e de 600 segundos podem ficar em branco. Para o grupo Telegram,
o ritmo considera até 20 mensagens em 60 segundos e no máximo uma por segundo
no mesmo chat. O orçamento finito do escopo é calculado a partir da janela e do
ritmo autorizado, com tetos menores se definidos pelo responsável ou provedor.
O Telegram pode devolver `retry_after`; a outbox persiste essa espera para o destino.

Consultas e links têm orçamentos próprios. Ambos compartilham a quota Shopee por
minuto e a pausa persistida em caso de HTTP 429. Para assinar a operação contínua,
informe a quota por dia e por minuto da conta Shopee e a referência documental.
A proposta atual mantém essas informações pendentes. [Proposta DAILY_V3](PROPOSTA_CONTINUA_edd577ae-4325-4e6b-acc0-e6015627518d.json)

O PostgreSQL guarda cada candidato por identidade e escopo recorrente. A lista
do painel exibe até 50 registros por vez e mostra o total pendente. Pendências
continuam no escopo no dia seguinte. O worker alterna descoberta e revalidação
por `shopId` e `itemId` dentro do orçamento de consultas. Um candidato só segue
para a outbox com nova evidência vigente no piloto do dia. Resultado incerto
continua impedindo nova tentativa. A cadência atual de envio segue a política
do provedor e os limites adicionais aprovados no escopo.

[Limites oficiais do Telegram](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)
