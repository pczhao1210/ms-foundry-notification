import fcntl
import json
import os
import pty
import select
import shutil
import subprocess
import termios
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (
    "daily_collect rest_changes_today rest_changes_upcoming rest_changes_past rest_models rest_model rest_prices "
    "mcp_get_today_changes mcp_get_upcoming_changes mcp_get_past_changes mcp_search_models mcp_get_model mcp_get_model_prices"
).split()


@pytest.fixture
def deploy(tmp_path):
    commands = {
        "az": """printf 'az %s\\n' "$*" >>"$CALL_LOG"
case "$*" in
    'account list'*) printf '%s\\n' "$MOCK_SUBSCRIPTIONS" ;;
    'account set'*) exit 0 ;;
    'group list'*) printf 'existing-group\\nother-group\\n' ;;
    'group show'*) printf '%s\\n' "${MOCK_GROUP_LOCATION:-}" ;;
    'deployment sub create'*) exit 0 ;;
    'functionapp list-flexconsumption-locations --query [].name'*) printf 'eastus2\\nwestus2\\n' ;;
  'account show'*) printf 'offline-review\\n' ;;
  'ad signed-in-user show'*) printf 'offline-user\\n' ;;
  'role assignment list'*) printf 'Owner\\n' ;;
  'provider show'*) printf 'NotRegistered\\n' ;;
  'provider register'*) exit 0 ;;
  'functionapp list-flexconsumption-locations'*) printf '1\\n' ;;
  'deployment sub what-if'*) exit 0 ;;
    'deployment sub show'*)
        if [[ -n "${MOCK_DEPLOYMENT_READ_ERROR:-}" ]]; then
            printf '%s\\n' "$MOCK_DEPLOYMENT_READ_ERROR" >&2
            exit 1
        fi
        printf '%s\\n' "$MOCK_DEPLOYMENT_OUTPUTS" ;;
    'functionapp show'*)
        if [[ -n "${MOCK_FUNCTION_APP_READ_ERROR:-}" ]]; then
            printf '%s\\n' "$MOCK_FUNCTION_APP_READ_ERROR" >&2
            exit 1
        fi
        printf '%s\\n' "$MOCK_FUNCTION_APP" ;;
  'functionapp deployment source config-zip'*) exit 0 ;;
  'functionapp function list'*) printf '%s\\n' "$MOCK_FUNCTIONS" ;;
  *) printf 'unexpected az command: %s\\n' "$*" >&2; exit 99 ;;
esac
""",
        "curl": """printf 'curl %s\\n' "$*" >>"$CALL_LOG"
if [[ "$*" == *'/archive/'* ]]; then
    tar -cz -C "$MOCK_SOURCE_ROOT" --transform='s,^,offline-source/,' infra/main.bicep src/function_app.py
elif [[ "$*" == *'/runtime/webhooks/mcp'* ]]; then
  printf '%s' "${MOCK_MCP_CODE:-401}"
else
  printf '%s' "${MOCK_REST_CODE:-401}"
fi
""",
        "sleep": "exit 0\n",
    }
    for name, body in commands.items():
        command = tmp_path / name
        command.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        command.chmod(0o755)
    log = tmp_path / "calls.log"

    def run(*args, answers=None, environment="review", piped=False, **overrides):
        env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALL_LOG": str(log),
               "MOCK_SOURCE_ROOT": str(ROOT),
               "AZURE_SUBSCRIPTION_ID": "", "AZURE_LOCATION": "eastus2",
               "AZURE_ENV_NAME": "", "AZURE_RESOURCE_GROUP": "", "AZURE_RESOURCE_NAME_PREFIX": "",
               "AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME": "",
               "MOCK_SUBSCRIPTIONS": '[{"name":"Offline review","id":"offline-review"}]',
               "MOCK_DEPLOYMENT_OUTPUTS": json.dumps({"state": "Succeeded", "outputs": {
                   "AZURE_RESOURCE_GROUP": {"type": "String", "value": "review-group"},
                   "AZURE_FUNCTION_APP_NAME": {"type": "String", "value": "review-app"},
               }}),
               "MOCK_FUNCTION_APP": json.dumps({"properties": {"defaultHostName": "offline.invalid"}}),
               "MOCK_FUNCTIONS": "\n".join(f"review-app/{name}" for name in FUNCTIONS), **overrides}
        if "DAILY_COLLECT_SCHEDULE" not in overrides:
            env.pop("DAILY_COLLECT_SCHEDULE", None)
        environment_args = ["-e", environment] if environment is not None else []
        command = ["bash", *(["-s", "--"] if piped else [str(ROOT / "deploy.sh")]), *environment_args, *args]
        script = (ROOT / "deploy.sh").read_text() if piped else None
        if answers is None:
            result = subprocess.run(command, cwd=ROOT, env=env, input=script,
                                    text=True, capture_output=True, timeout=30)
        else:
            master, slave = pty.openpty()

            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(1, termios.TIOCSCTTY, 0)

            process = subprocess.Popen(command, cwd=ROOT, env=env,
                                       stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                                       stdout=slave, stderr=slave, preexec_fn=controlling_terminal)
            os.close(slave)
            output = b""
            pending = b""
            responses = iter(answers)
            deadline = time.monotonic() + 20
            try:
                if piped:
                    process.stdin.write(script.encode())
                    process.stdin.close()
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if not ready:
                        if process.poll() is not None:
                            break
                        continue
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    pending += chunk
                    if b"Enter to keep: " in pending or b"[y/N] " in pending:
                        os.write(master, (next(responses) + "\n").encode())
                        pending = b""
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
            result = subprocess.CompletedProcess(command, process.returncode, output.decode(), output.decode())
        return result, log.read_text().splitlines() if log.exists() else []

    return run


def test_help_is_bilingual_and_does_not_call_azure(deploy):
    result, calls = deploy("--help")
    assert result.returncode == 0, result.stderr
    for chinese, english in [
        ("用法", "Usage"),
        ("环境名", "Environment name"),
        ("部署区域", "Deployment region"),
        ("资源组", "Resource group"),
        ("资源名称前缀", "Resource name prefix"),
        ("目标订阅", "Target subscription"),
        ("六字段 NCRONTAB", "six-field UTC NCRONTAB"),
        ("所需权限", "Required permissions"),
    ]:
        assert chinese in result.stdout and english in result.stdout
    assert not calls


@pytest.mark.parametrize("piped", [False, True])
def test_deployment_prompts_and_summary_are_bilingual(deploy, piped):
    result, _ = deploy(answers=["", "", "", "", "y"], piped=piped)
    assert result.returncode == 0, result.stderr
    for chinese, english in [
        ("使用唯一可用订阅", "Using the only enabled subscription"),
        ("选择区域", "Select a region"),
        ("选择资源组", "Select a resource group"),
        ("输入资源名称前缀", "Enter a resource name prefix"),
        ("触发时间 (UTC)", "Schedule (UTC)"),
        ("回车保留", "Enter to keep"),
        ("确认部署?", "Confirm deployment?"),
        ("部署基础设施", "Deploying infrastructure"),
        ("发布代码到", "Publishing code to"),
        ("鉴权检查通过", "Authentication check passed"),
        ("部署完成", "Deployment complete"),
        ("获取/创建密钥", "Get/create keys"),
    ]:
        assert chinese in result.stdout and english in result.stdout


@pytest.mark.parametrize("args, chinese, english", [
    (("--unknown",), "未知参数", "Unknown argument"),
    (("--skip-infra", "--skip-code"), "不能同时使用", "cannot be used together"),
    (("--what-if", "--schedule", "0 0 * * *"), "六字段 NCRONTAB", "six-field UTC NCRONTAB"),
])
def test_deployment_validation_errors_are_bilingual(deploy, args, chinese, english):
    result, calls = deploy(*args)
    assert result.returncode != 0
    assert chinese in result.stderr and english in result.stderr
    assert not any(call.startswith("az deployment") for call in calls)


def test_what_if_never_registers_providers_or_deploys_code(deploy):
    result, calls = deploy("--what-if")
    assert result.returncode == 0, result.stderr
    assert "尚未注册" in result.stderr and "is not registered" in result.stderr
    assert any(call.startswith("az provider show") for call in calls)
    assert any(call.startswith("az deployment sub what-if") for call in calls)
    assert not any(call.startswith("az provider register") for call in calls)
    assert not any("config-zip" in call for call in calls)


def test_alerts_are_opt_in_and_preview_does_not_write(deploy):
    result, calls = deploy("--what-if", "--enable-alerts")
    assert result.returncode == 0, result.stderr
    assert any("enableCollectionAlerts=true" in call for call in calls)
    assert not any(call.startswith("az provider register") for call in calls)


def test_alerts_default_to_disabled(deploy):
    result, calls = deploy("--what-if")
    assert result.returncode == 0, result.stderr
    assert any("enableCollectionAlerts=false" in call for call in calls)


@pytest.mark.parametrize("args, overrides, expected", [
    ((), {}, "0 0 0 * * *"),
    (("--schedule", "0 30 1 * * *"), {}, "0 30 1 * * *"),
    ((), {"DAILY_COLLECT_SCHEDULE": "0 0 16 * * *"}, "0 0 16 * * *"),
    (("--schedule", "0 30 1 * * *"), {"DAILY_COLLECT_SCHEDULE": "0 0 16 * * *"}, "0 30 1 * * *"),
])
def test_collection_schedule_is_forwarded(deploy, args, overrides, expected):
    result, calls = deploy("--what-if", *args, **overrides)
    assert result.returncode == 0, result.stderr
    deployment = next(call for call in calls if call.startswith("az deployment sub what-if"))
    assert f"dailyCollectSchedule={expected}" in deployment


@pytest.mark.parametrize("schedule", ["", "0 0 * * *", "0 0 0 * * *\nextra"])
def test_collection_schedule_requires_six_fields(deploy, schedule):
    result, _ = deploy("--what-if", DAILY_COLLECT_SCHEDULE=schedule)
    assert result.returncode != 0
    assert "六字段 NCRONTAB" in result.stderr


@pytest.mark.parametrize("args, overrides", [
    (("--schedule", "0 30 1 * * *"), {}),
    ((), {"DAILY_COLLECT_SCHEDULE": "0 0 0 * * *"}),
])
def test_collection_schedule_cannot_be_ignored_when_skipping_infra(deploy, args, overrides):
    result, _ = deploy("--skip-infra", "-y", *args, **overrides)
    assert result.returncode != 0
    assert "需要部署基础设施" in result.stderr


def test_subscription_deployment_keeps_provider_validation(deploy):
    result, calls = deploy("--skip-code", "-y")
    assert result.returncode == 0, result.stderr
    deployment = next(call for call in calls if call.startswith("az deployment sub create"))
    assert "--validation-level" not in deployment
    assert "resourceGroupName=rg-review" in deployment
    assert "subscriptionReaderAssignmentName=" in deployment.split()
    assert not any(call.startswith("az group create") for call in calls)


@pytest.mark.parametrize("args", [("--what-if",), ("--skip-code", "-y")])
def test_existing_subscription_reader_assignment_is_forwarded(deploy, args):
    assignment_name = "11111111-1111-1111-1111-111111111111"
    result, calls = deploy(*args, AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME=assignment_name)
    assert result.returncode == 0, result.stderr
    deployment = next(call for call in calls if call.startswith(("az deployment sub create",
                                                                "az deployment sub what-if")))
    assert f"subscriptionReaderAssignmentName={assignment_name}" in deployment.split()
    assert not any(call.startswith("az role assignment delete") for call in calls)


@pytest.fixture(scope="module")
def infrastructure_template():
    compiler = Path(shutil.which("bicep") or Path.home() / ".azure" / "bin" / "bicep")
    if not compiler.is_file():
        pytest.skip("Bicep compiler is required for the offline infrastructure contract test")
    result = subprocess.run([str(compiler), "build", str(ROOT / "infra/main.bicep"), "--stdout"],
                            env={**os.environ, "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"},
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_user_storage_roles_cover_blob_queue_and_table(infrastructure_template):
    template = infrastructure_template
    assert template["parameters"]["principalId"]["defaultValue"] == ""
    module = next(resource for resource in template["resources"] if resource["name"] == "rbac")
    assert module["properties"]["parameters"]["userPrincipalId"]["value"] == "[parameters('principalId')]"
    nested = module["properties"]["template"]
    assert nested["variables"]["roles"]["storageBlobDataContributor"] == "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    assert nested["variables"]["roles"]["storageQueueDataContributor"] == "974c5e8b-45b9-4653-ba55-5f855dd0fb88"
    assert nested["variables"]["roles"]["storageTableDataContributor"] == "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
    assert nested["variables"]["userStorageRoles"] == (
        "[if(empty(parameters('userPrincipalId')), createArray(), "
        "createArray(variables('roles').storageBlobDataContributor, "
        "variables('roles').storageQueueDataContributor, "
        "variables('roles').storageTableDataContributor))]"
    )
    assignment = next(resource for resource in nested["resources"]
                      if resource.get("copy", {}).get("name") == "userStorage")
    assert assignment["scope"] == (
        "[resourceId('Microsoft.Storage/storageAccounts', parameters('storageAccountName'))]"
    )
    assert assignment["properties"]["principalId"] == "[parameters('userPrincipalId')]"
    assert assignment["properties"]["principalType"] == "User"


def test_subscription_reader_assignment_template_tracks_principal(infrastructure_template):
    template = infrastructure_template
    reader = next(resource for resource in template["resources"]
                  if resource["name"] == "[format('subscription-reader-{0}', variables('resourceSuffix'))]")
    params = reader["properties"]["parameters"]
    assert "'identity'" in params["identityPrincipalId"]["value"]
    assert params["identityPrincipalId"]["value"].endswith(".outputs.principalId.value]")
    assert params["existingAssignmentName"]["value"] == "[parameters('subscriptionReaderAssignmentName')]"
    assert template["parameters"]["subscriptionReaderAssignmentName"]["defaultValue"] == ""
    nested = reader["properties"]["template"]
    assert "subscriptionDeploymentTemplate" in nested["$schema"]
    assert nested["variables"]["readerRoleId"] == "acdd72a7-3385-48ef-bd42-f606fba81ae7"
    assignment, = nested["resources"]
    assert assignment["type"] == "Microsoft.Authorization/roleAssignments"
    assert assignment["name"] == (
        "[if(empty(parameters('existingAssignmentName')), "
        "guid(subscription().id, parameters('identityPrincipalId'), variables('readerRoleId')), "
        "parameters('existingAssignmentName'))]"
    )
    assert assignment["properties"]["principalId"] == "[parameters('identityPrincipalId')]"
    assert assignment["properties"]["principalType"] == "ServicePrincipal"
    assert assignment["properties"]["roleDefinitionId"] == (
        "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('readerRoleId'))]"
    )


def test_portal_test_run_cors_allows_only_azure_portal(infrastructure_template):
    module = next(resource for resource in infrastructure_template["resources"]
                  if resource["name"] == "functionapp")
    site = next(resource for resource in module["properties"]["template"]["resources"]
                if resource["type"] == "Microsoft.Web/sites")
    assert site["properties"]["siteConfig"]["cors"] == {
        "allowedOrigins": ["https://portal.azure.com"],
        "supportCredentials": False,
    }


def test_collection_schedule_template_reaches_app_setting(infrastructure_template):
    template = infrastructure_template
    assert template["parameters"]["dailyCollectSchedule"]["defaultValue"] == "0 0 0 * * *"
    module = next(resource for resource in template["resources"] if resource["name"] == "functionapp")
    assert module["properties"]["parameters"]["dailyCollectSchedule"]["value"] == (
        "[parameters('dailyCollectSchedule')]"
    )
    site = next(resource for resource in module["properties"]["template"]["resources"]
                if resource["type"] == "Microsoft.Web/sites")
    settings = {setting["name"]: setting["value"] for setting in site["properties"]["siteConfig"]["appSettings"]}
    assert settings["DAILY_COLLECT_SCHEDULE"] == "[parameters('dailyCollectSchedule')]"
    parameters = json.loads((ROOT / "infra/main.parameters.json").read_text())
    assert parameters["parameters"]["dailyCollectSchedule"]["value"] == "0 0 0 * * *"


def test_what_if_keeps_provider_validation(deploy):
    result, calls = deploy("--what-if")
    assert result.returncode == 0, result.stderr
    deployment = next(call for call in calls if call.startswith("az deployment sub what-if"))
    assert "--validation-level" not in deployment


@pytest.mark.parametrize("setting, code", [("MOCK_REST_CODE", "200"), ("MOCK_REST_CODE", "500"),
                                           ("MOCK_MCP_CODE", "200"), ("MOCK_MCP_CODE", "404")])
def test_authentication_failure_is_a_failed_deployment(deploy, setting, code):
    result, _ = deploy("--skip-infra", "-y", **{setting: code})
    assert result.returncode != 0
    assert "401" in result.stderr
    assert "az functionapp keys" not in result.stdout


def test_missing_mcp_function_is_a_failed_deployment(deploy):
    functions = "\n".join(f"review-app/{name}" for name in FUNCTIONS if name != "mcp_get_model")
    result, calls = deploy("--skip-infra", "-y", MOCK_FUNCTIONS=functions)
    assert result.returncode != 0
    assert "mcp_get_model" in result.stderr
    assert not any(call.startswith("curl ") for call in calls)


@pytest.mark.parametrize("host", [None, "", " ", "None", "null", 123, {}, [],
                                 "https://offline.invalid", "offline.invalid/api", "offline.invalid:443",
                                 "offline.invalid\n", "offline.invalid\tother.invalid", "user@offline.invalid",
                                 "-offline.invalid", "offline..invalid", "a" * 64 + ".invalid",
                                 ".".join(["a" * 63] * 4)])
@pytest.mark.parametrize("nested", [False, True])
def test_invalid_function_app_host_stops_before_publish(deploy, host, nested):
    response = {"defaultHostName": host}
    if nested:
        response = {"properties": response}
    result, calls = deploy("--skip-infra", "-y", MOCK_FUNCTION_APP=json.dumps(response))
    assert result.returncode != 0
    assert "主机名" in result.stderr
    assert not any("config-zip" in call or call.startswith("curl ") for call in calls)


@pytest.mark.parametrize("key", ["defaultHostName", "defaultHostname", "defaulthostname", "DEFAULTHOSTNAME"])
@pytest.mark.parametrize("piped", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_function_app_host_accepts_key_casing(deploy, key, piped, nested):
    host = "review-app-123.japaneast-01.azurewebsites.net"
    response = {key: host}
    if nested:
        response = {"name": "review-app", "type": "Microsoft.Web/sites", "properties": response}
    result, calls = deploy("--skip-infra", "-y", piped=piped,
                           MOCK_FUNCTION_APP=json.dumps(response))
    assert result.returncode == 0, result.stderr
    assert "az functionapp show -g review-group -n review-app -o json" in calls
    probes = [call for call in calls if call.startswith("curl ") and "/archive/" not in call]
    assert len(probes) == 2
    assert probes[0].endswith(f"https://{host}/api/changes/today")
    assert probes[1].endswith(f"https://{host}/runtime/webhooks/mcp")
    assert f"REST   : https://{host}/api/changes/today" in result.stdout
    assert f"MCP    : https://{host}/runtime/webhooks/mcp" in result.stdout


@pytest.mark.parametrize("response", ["", "not-json", "null", "[]", "{}",
                                     '{"other":"must-not-log-value"}',
                                     '{"defaultHostName":"offline.invalid",'
                                     '"defaultHostname":"must-not-log-value"}',
                                     json.dumps({"properties": None}),
                                     json.dumps({"properties": []}),
                                     json.dumps({"properties": 123}),
                                     json.dumps({"properties": "must-not-log-value"}),
                                     json.dumps({"properties": {}}),
                                     json.dumps({"properties": {"other": "must-not-log-value"}}),
                                     json.dumps({"properties": {"defaultHostName": "offline.invalid",
                                                                "defaultHostname": "must-not-log-value"}}),
                                     json.dumps({"defaultHostName": "offline.invalid",
                                                 "properties": {"defaultHostName": "must-not-log-value"}}),
                                     json.dumps({"defaultHostName": "must-not-log-value",
                                                 "properties": {"defaultHostName": "must-not-log-value"}}),
                                     json.dumps({"properties": {"siteConfig": {
                                         "defaultHostName": "must-not-log-value"}}})])
def test_invalid_function_app_response_stops_before_publish(deploy, response):
    result, calls = deploy("--skip-infra", "-y", MOCK_FUNCTION_APP=response)
    assert result.returncode != 0
    assert "主机名" in result.stderr
    assert "must-not-log-value" not in result.stdout + result.stderr
    assert not any("config-zip" in call or call.startswith("curl ") for call in calls)


def test_function_app_read_failure_stops_before_publish(deploy):
    result, calls = deploy("--skip-infra", "-y", MOCK_FUNCTION_APP_READ_ERROR="offline lookup failed")
    assert result.returncode != 0
    assert "offline lookup failed" in result.stderr
    assert "无法读取 Function App" in result.stderr
    assert not any("config-zip" in call or call.startswith("curl ") for call in calls)


def test_success_requires_all_functions_and_both_authenticated_endpoints(deploy):
    result, calls = deploy("--skip-infra", "-y")
    assert result.returncode == 0, result.stderr
    assert any("config-zip" in call and "--build-remote true" in call for call in calls)
    probes = [call for call in calls if call.startswith("curl ")]
    assert len(probes) == 2
    assert all("--connect-timeout 10 --max-time 30" in call for call in probes)
    assert probes[0].endswith("https://offline.invalid/api/changes/today")
    assert probes[1].endswith("https://offline.invalid/runtime/webhooks/mcp") and "initialize" in probes[1]
    assert not any(call.startswith("az functionapp keys") for call in calls)


@pytest.mark.parametrize("answers, expected", [
    (["", "", "", "", "y"], ("eastus2", "rg-review", "review")),
    (["2", "1", "demo", "", "y"], ("westus2", "existing-group", "demo")),
    (["", "", "123", "", "y"], ("eastus2", "rg-review", "123")),
    (["99", "westus2", "new-group", "new-prefix", "", "y"], ("westus2", "new-group", "new-prefix")),
])
def test_interactive_options_use_terminal_and_reach_bicep(deploy, answers, expected):
    result, calls = deploy("--skip-code", answers=answers)
    assert result.returncode == 0, result.stderr
    location, group, prefix = expected
    deployment = next(call for call in calls if call.startswith("az deployment sub create"))
    assert f"location={location}" in deployment
    assert f"resourceGroupName={group}" in deployment
    assert f"resourceNamePrefix={prefix}" in deployment
    assert result.stdout.index("1/4") < result.stdout.index("2/4") < result.stdout.index("3/4") < result.stdout.index("4/4")
    assert "dailyCollectSchedule=0 0 0 * * *" in deployment


@pytest.mark.parametrize("piped", [False, True])
@pytest.mark.parametrize("answer, expected", [("", "0 0 0 * * *"), ("0 30 1 * * *", "0 30 1 * * *")])
def test_interactive_schedule_defaults_or_overrides_without_flag(deploy, piped, answer, expected):
    result, calls = deploy("--skip-code", piped=piped, answers=["", "", "", answer, "y"])
    assert result.returncode == 0, result.stderr
    assert "触发时间 (UTC) / Schedule (UTC) [0 0 0 * * *]" in result.stdout
    deployment = next(call for call in calls if call.startswith("az deployment sub create"))
    assert f"dailyCollectSchedule={expected}" in deployment


@pytest.mark.parametrize("args, overrides", [
    (("--schedule", "0 30 1 * * *"), {}),
    ((), {"DAILY_COLLECT_SCHEDULE": "0 30 1 * * *"}),
])
def test_interactive_schedule_enter_preserves_preset(deploy, args, overrides):
    result, calls = deploy("--skip-code", *args, answers=["", "", "", "", "y"], **overrides)
    assert result.returncode == 0, result.stderr
    assert "触发时间 (UTC) / Schedule (UTC) [0 30 1 * * *]" in result.stdout
    assert any("dailyCollectSchedule=0 30 1 * * *" in call for call in calls)


def test_invalid_interactive_schedule_stops_before_deployment(deploy):
    result, calls = deploy("--skip-code", answers=["", "", "", "0 0 * * *"])
    assert result.returncode != 0
    assert "六字段 NCRONTAB" in result.stderr
    assert not any(call.startswith("az deployment sub create") for call in calls)


def test_yes_uses_explicit_options_without_wizard(deploy):
    result, calls = deploy("--skip-code", "-y", "-g", "custom-group", "--resource-prefix", "custom")
    assert result.returncode == 0, result.stderr
    deployment = next(call for call in calls if call.startswith("az deployment sub create"))
    assert "resourceGroupName=custom-group" in deployment and "resourceNamePrefix=custom" in deployment
    assert not any(call.startswith("az group list") for call in calls)
    assert "1/4" not in result.stdout


def test_existing_resource_group_keeps_its_metadata_location(deploy):
    result, calls = deploy("--what-if", "-g", "existing-group", MOCK_GROUP_LOCATION="westus2")
    assert result.returncode == 0, result.stderr
    deployment = next(call for call in calls if call.startswith("az deployment sub what-if"))
    assert "location=eastus2" in deployment
    assert "resourceGroupLocation=westus2" in deployment


@pytest.mark.parametrize("piped", [False, True])
def test_default_environment_needs_no_arguments(deploy, piped):
    result, calls = deploy("--skip-code", environment=None, piped=piped, answers=["2", "", "", "", "", "y"],
                           MOCK_SUBSCRIPTIONS='[{"name":"Current","id":"offline-review"},'
                                              '{"name":"Other","id":"offline-other"}]')
    assert result.returncode == 0, result.stderr
    assert "az account set --subscription offline-other" in calls
    deployment = next(call for call in calls if call.startswith("az deployment sub create"))
    arguments = deployment.split()
    assert arguments[arguments.index("--location") + 1] == "eastus2"
    assert "environmentName=foundry-notify" in arguments
    assert "location=eastus2" in arguments
    assert "resourceGroupName=rg-foundry-notify" in arguments
    assert "resourceGroupLocation=eastus2" in arguments
    assert "resourceNamePrefix=foundry-notify" in arguments
    if piped:
        assert any("/archive/main.tar.gz" in call for call in calls)


@pytest.mark.parametrize("group, app", [(None, None), (None, "review-app"), ("review-group", None),
                                        ("None", "review-app"), ("review-group", "null"),
                                        ("", "review-app"), ("review-group", " "),
                                        ({"value": "review-group"}, "review-app"),
                                        ("review-group", 123)])
def test_invalid_deployment_outputs_stop_before_function_lookup(deploy, group, app):
    outputs = {"state": "Succeeded", "outputs": {
        "AZURE_RESOURCE_GROUP": {"type": "String", "value": group},
        "AZURE_FUNCTION_APP_NAME": {"type": "String", "value": app},
    }}
    result, calls = deploy("--skip-code", environment=None, answers=["", "", "", "", "y"],
                           MOCK_DEPLOYMENT_OUTPUTS=json.dumps(outputs))
    assert result.returncode != 0
    assert "部署输出" in result.stderr
    assert not any(call.startswith("az functionapp show") for call in calls)


@pytest.mark.parametrize("outputs", [None, {}, {"AZURE_RESOURCE_GROUP": {"value": "review-group"}}])
def test_missing_deployment_outputs_stop_code_only_deployment(deploy, outputs):
    result, calls = deploy("--skip-infra", "-y",
                           MOCK_DEPLOYMENT_OUTPUTS=json.dumps({"state": "Succeeded", "outputs": outputs}))
    assert result.returncode != 0
    assert "部署输出无效" in result.stderr
    assert not any(call.startswith("az functionapp") for call in calls)


@pytest.mark.parametrize("state", ["Failed", "Running", None])
def test_unsuccessful_deployment_stops_before_function_lookup(deploy, state):
    result, calls = deploy("--skip-infra", "-y",
                           MOCK_DEPLOYMENT_OUTPUTS=json.dumps({"state": state, "outputs": {}}))
    assert result.returncode != 0
    assert "不是 Succeeded" in result.stderr
    assert not any(call.startswith("az functionapp") for call in calls)


@pytest.mark.parametrize("group_key, app_key", [
    ("AZURE_RESOURCE_GROUP", "AZURE_FUNCTION_APP_NAME"),
    ("azurE_RESOURCE_GROUP", "azurE_FUNCTION_APP_NAME"),
    ("azure_resource_group", "azure_function_app_name"),
])
def test_deployment_outputs_accept_key_casing(deploy, group_key, app_key):
    response = {"state": "Succeeded", "outputs": {
        group_key: {"type": "String", "value": "review-group"},
        app_key: {"type": "String", "value": "review-app"},
        "storagE_BLOB_ENDPOINT": {"type": "String", "value": "https://offline.invalid/"},
    }}
    result, calls = deploy("--skip-infra", "-y", MOCK_DEPLOYMENT_OUTPUTS=json.dumps(response))
    assert result.returncode == 0, result.stderr
    outputs = next(call for call in calls if call.startswith("az deployment sub show"))
    assert "--name foundry-notify-review" in outputs
    assert "-o json" in outputs
    assert "outputs:properties.outputs" in outputs
    assert "资源组 / Resource group=review-group   Function App=review-app" in result.stdout
    assert "az functionapp show -g review-group -n review-app -o json" in calls
    assert any("config-zip -g review-group -n review-app" in call for call in calls)


@pytest.mark.parametrize("key", ["AZURE_RESOURCE_GROUP", "AZURE_FUNCTION_APP_NAME"])
def test_deployment_outputs_reject_case_collisions(deploy, key):
    response = {"state": "Succeeded", "outputs": {
        "AZURE_RESOURCE_GROUP": {"type": "String", "value": "review-group"},
        "AZURE_FUNCTION_APP_NAME": {"type": "String", "value": "review-app"},
        key.lower(): {"type": "String", "value": "must-not-log-value"},
    }}
    result, calls = deploy("--skip-infra", "-y", MOCK_DEPLOYMENT_OUTPUTS=json.dumps(response))
    assert result.returncode != 0
    assert f"部署输出 {key} 存在大小写冲突" in result.stderr
    assert "must-not-log-value" not in result.stdout + result.stderr
    assert not any(call.startswith("az functionapp") for call in calls)


def test_missing_output_reports_field_names_without_values(deploy):
    outputs = {"state": "Succeeded", "outputs": {
        "unexpectedOutput": {"type": "String", "value": "must-not-log-value"},
    }}
    result, calls = deploy("--skip-infra", "-y", MOCK_DEPLOYMENT_OUTPUTS=json.dumps(outputs))
    assert result.returncode != 0
    assert 'ARM 实际输出字段: ["unexpectedOutput"]' in result.stderr
    assert "must-not-log-value" not in result.stdout + result.stderr
    assert not any(call.startswith("az functionapp") for call in calls)


def test_deployment_read_failure_preserves_error_and_stops_before_publish(deploy):
    result, calls = deploy("--skip-infra", "-y", MOCK_DEPLOYMENT_READ_ERROR="AuthorizationFailed: offline denial")
    assert result.returncode != 0
    assert "AuthorizationFailed: offline denial" in result.stderr
    assert "无法读取部署" in result.stderr
    assert not any(call.startswith("az functionapp") for call in calls)


def test_invalid_output_reports_structure_without_values(deploy):
    outputs = {"state": "Succeeded", "outputs": {
        "AZURE_RESOURCE_GROUP": {"type": "String", "unexpectedValue": "must-not-log-value"},
    }}
    result, calls = deploy("--skip-infra", "-y", MOCK_DEPLOYMENT_OUTPUTS=json.dumps(outputs))
    assert result.returncode != 0
    assert '项目字段: ["type", "unexpectedValue"]' in result.stderr
    assert "value 类型: NoneType" in result.stderr
    assert "must-not-log-value" not in result.stdout + result.stderr
    assert not any(call.startswith("az functionapp") for call in calls)


def test_cancelled_wizard_does_not_deploy(deploy):
    result, calls = deploy("--skip-code", answers=["", "", "", "", "n"])
    assert result.returncode != 0
    assert not any(call.startswith("az deployment sub create") for call in calls)
    assert not any(call.startswith("az provider register") for call in calls)


@pytest.mark.parametrize("args", [("-g", "bad/group"), ("--resource-prefix", "UPPER"),
                                  ("-l", "invalid'query")])
def test_invalid_resource_options_fail_before_deployment(deploy, args):
    result, calls = deploy("--what-if", *args)
    assert result.returncode != 0
    assert not any(call.startswith("az deployment sub") for call in calls)


@pytest.mark.parametrize("answer, selected", [("2", "offline-other"), ("", "offline-review"),
                                             ("offline-other", "offline-other")])
def test_multiple_subscriptions_are_selected_before_regions(deploy, answer, selected):
    result, calls = deploy("--skip-code", answers=[answer, "", "", "", "", "y"],
                           MOCK_SUBSCRIPTIONS='[{"name":"Same name","id":"offline-review"},'
                                              '{"name":"Same name","id":"offline-other"}]')
    assert result.returncode == 0, result.stderr
    account_set = calls.index(f"az account set --subscription {selected}")
    regions = next(index for index, call in enumerate(calls) if call.startswith("az functionapp list-flex"))
    groups = next(index for index, call in enumerate(calls) if call.startswith("az group list"))
    assert account_set < regions < groups
    assert result.stdout.index("选择订阅") < result.stdout.index("1/4")
    assert "Same name (offline-review)" in result.stdout and "Same name (offline-other)" in result.stdout


def test_single_subscription_does_not_prompt(deploy):
    result, calls = deploy("--skip-code", answers=["", "", "", "", "y"])
    assert result.returncode == 0, result.stderr
    assert "使用唯一可用订阅" in result.stdout
    assert "订阅 / Subscription [" not in result.stdout
    assert "az account set --subscription offline-review" in calls


def test_default_subscription_is_current_not_first_in_list(deploy):
    result, calls = deploy("--skip-code", answers=["", "", "", "", "", "y"],
                           MOCK_SUBSCRIPTIONS='[{"name":"Other","id":"offline-other"},'
                                              '{"name":"Current","id":"offline-review"}]')
    assert result.returncode == 0, result.stderr
    assert "订阅 / Subscription [Current (offline-review)]" in result.stdout
    assert "az account set --subscription offline-review" in calls


def test_code_only_deployment_selects_subscription_before_reading_outputs(deploy):
    result, calls = deploy("--skip-infra", answers=["2", "y"],
                           MOCK_SUBSCRIPTIONS='[{"name":"Current","id":"offline-review"},'
                                              '{"name":"Other","id":"offline-other"}]')
    assert result.returncode == 0, result.stderr
    selected = calls.index("az account set --subscription offline-other")
    outputs = next(index for index, call in enumerate(calls) if call.startswith("az deployment sub show"))
    assert selected < outputs
    assert "1/4" not in result.stdout
    assert "触发时间 (UTC) / Schedule (UTC) [" not in result.stdout


@pytest.mark.parametrize("args, overrides", [(("-s", "offline-other"), {}),
                                           ((), {"AZURE_SUBSCRIPTION_ID": "offline-other"})])
def test_explicit_subscription_skips_subscription_prompt(deploy, args, overrides):
    result, calls = deploy("--skip-code", *args, answers=["", "", "", "", "y"], **overrides)
    assert result.returncode == 0, result.stderr
    assert "az account set --subscription offline-other" in calls
    assert not any(call.startswith("az account list") for call in calls)


@pytest.mark.parametrize("args", [("--what-if",), ("--skip-code", "-y")])
def test_noninteractive_modes_do_not_list_subscriptions(deploy, args):
    result, calls = deploy(*args)
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("az account list") for call in calls)


def test_no_enabled_subscription_stops_before_region_selection(deploy):
    result, calls = deploy("--skip-code", answers=[], MOCK_SUBSCRIPTIONS="[]")
    assert result.returncode != 0
    assert "没有可用订阅" in result.stderr
    assert not any(call.startswith("az functionapp list-flex") for call in calls)