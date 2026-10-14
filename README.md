# Codex Pro Tasks

Coordenador privado para distribuir objetivos entre agentes locais que usam o **Codex CLI com login do ChatGPT**. Cada agente utiliza a assinatura e os limites da conta autenticada em seu próprio ambiente. O coordenador não usa chaves de API, não controla o navegador do ChatGPT e não recebe credenciais das contas.

Você envia um objetivo pelo painel. Um agente pode dividi-lo em até oito tarefas; os agentes disponíveis recebem as tarefas da fila. Cada tentativa trabalha em um clone e branch separados, executa as verificações configuradas e, quando solicitado, publica a branch e abre um pull request em rascunho. Não faz merge automático na branch principal.

**Estado desta entrega:** fila, HTTP, painel e execução de comandos/Git foram testados localmente. Chamadas Codex foram substituídas por um simulador offline nos testes automatizados. Nenhuma das suas contas Pro está conectada ainda, e não foi realizada uma tarefa real consumindo sua assinatura. A ativação exige instalar e autenticar o CLI em cada agente.

## O que precisa estar instalado

- No coordenador: Python **3.10+**. Não há dependências Python externas para executar o sistema.
- Nos agentes: Python 3.10+, Git e **Codex CLI oficial atualizado**. Node.js compatível com o pacote oficial, se a instalação for por npm.
- Para publicar pull requests no GitHub: Git autenticado no repositório e GitHub CLI (`gh`) autenticado naquele ambiente.
- Um PC, servidor privado, usuário de sistema ou VM por ambiente de conta. O app do ChatGPT no celular sozinho não executa estes agentes.

Linux foi usado nos testes. macOS usa a mesma linha de comando; no Windows, prefira WSL2. Não há distribuição Docker nem serviço hospedado já ativado nesta entrega.

## 1. Iniciar o coordenador

Dentro da pasta do projeto:

```bash
python3 -m codex_tasks init
python3 -m codex_tasks server
```

Na **sua máquina**, abra `http://127.0.0.1:8765`. Entre usando o campo `admin_token` de `.state/server.json`. Esse token é a credencial deste painel, **não** uma senha/token do ChatGPT. Os arquivos de estado são privados e ignorados pelo Git.

O servidor escuta somente em loopback. Você pode usar `--port 8766` para outra porta. O painel permite cadastrar projetos, agentes, objetivos, acompanhar resultados, cancelar tarefas e retomar tarefas interrompidas explicitamente.

## 2. Cadastrar o projeto atendido

No painel, em **Projetos atendidos**, informe:

- Um identificador, como `meu-projeto`.
- A URL HTTPS do GitHub, como `https://github.com/SUA_CONTA/SEU_REPOSITORIO.git`.
- A branch base, normalmente `main`.
- Comandos de verificação como listas de argumentos em JSON, por exemplo:

```json
[["python3", "-m", "unittest", "discover", "-s", "tests"], ["npm", "test"]]
```

Só configure comandos que existam e façam sentido naquele projeto. Comandos não são recebidos da resposta do modelo e não são executados por shell. Dependências e serviços necessários aos testes devem estar preparados no ambiente de cada agente. Sem comandos configurados, o resultado informa zero verificações; isso não significa que o projeto foi testado.

O campo **Instruções compartilhadas** adiciona orientações a todas as tarefas. O Codex também lê o `AGENTS.md` do projeto atendido. Para continuidade entre contas, registre decisões e requisitos no próprio repositório; histórico completo de chats não é sincronizado automaticamente.

Também é possível atender um repositório Git local usando caminho absoluto; esse mesmo caminho precisa existir nos agentes que o atenderem. Não há pull request GitHub para um remoto local.

## 3. Conectar cada conta Pro

No ambiente próprio da primeira conta:

```bash
npm install -g @openai/codex
codex login
codex login status
```

Entre pelo ChatGPT usando essa conta. Em um servidor sem navegador, use o fluxo oficial:

```bash
codex login --device-auth
```

No painel do coordenador, cadastre `pro-1` e selecione os projetos autorizados. Ele exibirá uma configuração uma única vez. Salve-a como `worker-config.json` no ambiente do agente. **O campo `token` desse arquivo conecta ao coordenador; não coloque nele uma credencial OpenAI.**

Depois execute, dentro da pasta deste projeto:

```bash
python3 -m codex_tasks doctor
python3 -m codex_tasks worker --config worker-config.json
```

Repita para `pro-2`, `pro-3` etc., cada um em seu usuário de sistema/VM/computador e com seu login ChatGPT normal. Não copie `auth.json`, não compartilhe senhas e não altere HOME/CODEX_HOME para alternar contas. O servidor só guarda o hash do token de conexão de cada agente.

O modo de autenticação é conferido pelo `codex login status`, e as execuções impõem `forced_login_method="chatgpt"` e `model_provider="openai"`. O agente recusa iniciar quando encontra `OPENAI_API_KEY` ou `CODEX_API_KEY` no ambiente. Não há fallback para cobrança de API. O sistema não consulta seu tipo de plano nem o saldo exato de uso: esses limites são geridos pelo Codex.

Para executar no máximo uma tarefa e sair:

```bash
python3 -m codex_tasks worker --config worker-config.json --once
```

## Agentes em outras máquinas

Use um túnel SSH para manter o coordenador privado. No ambiente do agente:

```bash
ssh -N -L 8765:127.0.0.1:8765 USUARIO@MAQUINA_DO_COORDENADOR
```

Mantenha esse terminal aberto. A configuração do agente continua com `http://127.0.0.1:8765`, agora encaminhado pelo SSH. Se já houver algo nessa porta local, escolha outra porta do lado esquerdo e ajuste a URL do agente. HTTPS com certificado válido também é aceito quando você já tem um proxy privado configurado; HTTP sem TLS fora de loopback é recusado. Não foi configurado proxy, VPN ou exposição pública automaticamente.

## 4. Enviar trabalho

No painel, escolha o projeto, título e objetivo.

- **Dividir em tarefas e distribuir** usa uma primeira execução Codex em sandbox `read-only` para produzir o plano. O plano válido cria as tarefas automaticamente, com dependências quando necessário.
- **Executar como uma tarefa** envia diretamente uma tarefa de código.
- **Publicar branches e criar pull requests em rascunho** autoriza push da branch da tarefa e abertura de PR. Deixe desmarcado para manter mudanças só no clone privado do agente.

As tarefas são entregues por ordem da fila ao primeiro agente disponível e autorizado para o projeto. Cada agente executa uma tarefa de cada vez. Não há previsão fictícia de progresso nem união automática dos limites das contas.

Quando uma tarefa depende de outra com mudanças, os próximos agentes precisam da branch publicada. Eles fazem merge da dependência **apenas na branch da tarefa**, nunca na principal. Se houver conflito, a execução falha e preserva o checkout para diagnóstico. Planos com dependências e publicação desmarcada são recusados para não criar uma fila que não possa avançar.

O Codex executa com `workspace-write` e aprovação `never`: um comando que necessite permissão adicional deve falhar, em vez de abrir um pedido que ninguém consiga responder. O executor não usa `danger-full-access` nem opções de bypass de sandbox. Os comandos de verificação cadastrados são executados pelo agente como processos normais, fora do sandbox Codex; use somente comandos autorizados em ambientes privados que você controla.

## Limites, desconexão e retomada

Ao receber um erro identificável de limite de uso, o agente é pausado e a tarefa fica vinculada à mesma conta/ambiente. Não troca automaticamente de conta para repetir essa tarefa. Quando o acesso dessa conta voltar, clique **Retomar tarefa** e **Retomar agente**, e reinicie o worker se ele tiver encerrado.

Se a conexão/heartbeat expirar, a tentativa antiga não pode registrar sucesso nem obter autorização para publicar. O agente encerra seus processos controlados e o painel deixa a tarefa interrompida. Confirme que a execução anterior parou antes de retomar. Cancelamento segue o mesmo mecanismo. Não há redistribuição automática de tentativas interrompidas, evitando trabalhos duplicados e gastos de uso não supervisionados.

O checkout, resultados e logs ficam no `work_dir` do agente. Não são enviados integralmente ao coordenador nem incluídos no Git deste sistema. O painel recebe somente resumo, branch, commit, verificações e link do PR. Conserve os arquivos privados necessários e remova tentativas antigas manualmente quando não precisar mais delas.

## Testar o sistema

```bash
python3 -m unittest discover -s tests -v
```

Os testes usam SQLite e HTTP reais, clones/commits/pushes Git locais e uma CLI Codex simulada explicitamente em `tests/fake_codex.py`. Conferem distribuição concorrente, dependências, login por assinatura, recusa de API, pausa por limite, leases, cancelamento, isolamento de branches, bloqueio de arquivos de credenciais e falhas de verificação sem publicação. Nenhuma conta real é acessada por esses testes.

Teste visual opcional, com Playwright e Chromium instalados:

```bash
python3 tests/browser_smoke.py
```

## Documentação oficial consultada

- [Autenticação do Codex: ChatGPT para acesso pela assinatura](https://developers.openai.com/codex/auth)
- [Execução não interativa: `codex exec` e autenticação CLI existente](https://developers.openai.com/codex/noninteractive)
- [Referência de comandos do Codex CLI](https://developers.openai.com/codex/cli/reference)

Essa arquitetura usa clientes locais e runners privados. Não fornece credenciais de conta a GitHub Actions públicos, não automatiza login/sessões pelo navegador e não promete acesso ilimitado ou equivalência entre todas as opções da interface web e do CLI.
