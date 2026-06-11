# FlakyDoctor <img padding="10" align="right" src="https://www.acm.org/binaries/content/gallery/acm/publications/artifact-review-v1_1-badges/artifacts_evaluated_functional_v1_1.png" alt="ACM Artifacts Evaluated - functional v1.1" width="40" height="40"/> <img padding="10" align="left" src="https://www.acm.org/binaries/content/gallery/acm/publications/artifact-review-v1_1-badges/artifacts_available_v1_1.png" alt="ACM Artifacts Available v1.1" width="40" height="40"/> 

This repo contains the source code and results of FlakyDoctor, a neuro-symbolic approach to fixing Implementation-Dependent (ID) and Order-Dependent (OD) tests.                                 
## 🌟 File structures
File structures in this repository are as follows, please refer to `README.md` in each directory for more details: 
- [datasets](datasets/): Datasets of flaky tests in the evaluation.
- [patches](patches/): Successful patches generated.
- [results](results/): Detailed results for successfully fixed flaky tests in the evaluation.
- [src](src/): Source code and scripts to run FlakyDoctor.

## 🌟 A quick demo to reproduce sample results

This section provides a quick demo using GPT-4 to reproduce sample results in ~40 minutes.

**0. Before starting:**
- FlakyDoctor works on `Linux` with the following environment:
```
Python 3.10.12
Java 8 and Java 11
Maven 3.6.3
```
- The current FlakyDoctor supports GPT-4 and Magicoder. Please prepare an [openai key](https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key) to use GPT-4; if you want to run Magicoder, download its [checkpoints](https://huggingface.co/ise-uiuc/Magicoder-S-DS-6.7B) into a local path. We use three NVIDIA GeForce RTX 3090 GPUs in our experiments.

**1. Set up requirements:**
```
git clone https://github.com/Intelligent-CAT-Lab/FlakyDoctor
cd FlakyDoctor
bash -x src/setup.sh |& tee setup.log
```
**2. Create a `.env` which includes your local path of model [Magicoder](https://huggingface.co/ise-uiuc/Magicoder-S-DS-6.7B) (you can skip this step if only running GPT-4):**
```
echo "Magicoder_LOAD_PATH=[Your local path of Magicoder checkpoints]" > .env
```

**3. Run the following commands to fix demo tests with GPT-4:** Please put your openai key at the placeholder.
```
# install Java projects
bash -x src/install.sh datasets/demo_projects.csv projects outputs install_summary.csv 
# fix flay tests 
bash -x src/run_FlakyDoctor.sh projects [openai_key] GPT-4 outputs datasets/demo.csv ID 
```
To check the outputs of the building project, logs of each round will be saved into a directory named `[unique SHA]` inside `outputs`. You can also check the summary of building results in `install_summary.csv`, including `project,sha,module,build_result,java_version`.  

To check the results of flakiness repair, each round, a directory named as `ID_Results_GPT-4_projects_[Unique SHA]` will be generated inside `outputs`:
- you may check instant logs in `ID_Results_GPT-4_projects_[Unique SHA]/[Unique SHA].log`; 
- you can see a summary of all results in `ID_Results_GPT-4_projects_[Unique SHA]/GPT-4_results_[Unique SHA].csv` or more details in `ID_Results_GPT-4_projects_[Unique SHA]/GPT-4_test_Details_[Unique SHA].json`. 
- If any successful patches are generated, they will be saved in `ID_Results_GPT-4_projects_[Unique SHA]/GoodPatches`. 
**Please note that the results may vary when running at multiple times due to the non-determinism of LLMs.**

## 🌟 Reproduce the results from scratch

To reproduce the results from scratch, one should run the following commands:

**0. Before starting:** 
- FlakyDoctor works on `Linux` with the following environment:
```
Python 3.10.12
Java 8 and Java 11
Maven 3.6.3
```
- Please also prepare an [openai key](https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key) and local checkpoints of [Magicoder](https://huggingface.co/ise-uiuc/Magicoder-S-DS-6.7B)

**1. Set up requirements:**
```
git clone https://github.com/Intelligent-CAT-Lab/FlakyDoctor
cd FlakyDoctor
bash -x src/setup.sh
```
**2. Create a `.env` which includes your local path of model [Magicoder](https://huggingface.co/ise-uiuc/Magicoder-S-DS-6.7B):**
```
echo "Magicoder_LOAD_PATH=[Your local path of Magicoder checkpoints]" > .env
```

**3. Clone and build all Java projects:**
To clone and build the projects, one should run the following commands:
```
bash -x src/install.sh [input_csv] [clone_dir] [output_dir] [save_csv]
```
- `input_csv`: Input of ID Java projects you need to set up, each line is in the format of `Project URL, SHA, Module`. More details in [datasets](datasets/README.md).
- `clone_dir`: A directory to clone all the java projects.
- `output_dir`: A directory for outputs and logs when building the projects.
- `save_csv`: A summary of the build results.

For example, one can run:
- `bash -x src/install.sh datasets/ID_projects.csv projects outputs ID_summary.csv` to build all Java projects for ID tests (~15 hours)
- `bash -x src/install.sh datasets/OD_projects.csv projects outputs OD_summary.csv` to build all Java projects for OD tests (~10 hours)

**4. Run FlakyDoctor to fix flaky tests:**
To fix flaky tests, one should run the following commands:
```
bash -x src/run_FlakyDoctor.sh [clone_dir] [openai_key] [model] [output_dir] [input_csv] [test_type]
```
- `clone_dir`: A directory where all the java projects are cloned.
- `openai_key`: Your openai authentication key.
- `model`: `GPT-4` or `MagiCoder`
- `output_dir`: A directory to save all the results.
- `input_csv`: An input `.csv` file that includes all the flaky tests. More details in [datasets](datasets/README.md).
- `test_type`: The type of flakiness to fix, `ID` or `OD`.

## 🌟 Running with Claude (incl. macOS)

This fork adds **Claude** (Anthropic) as a third model option next to GPT-4 and MagiCoder. The GPT-4/OpenAI path is unchanged; selecting `--model Claude` routes the same prompts to the Claude API (`claude-sonnet-4-6`, see `generate_prompts` in `src/repair_ID.py` / `src/repair_OD.py` to change the model id). Unlike MagicCoder, no GPU is needed; unlike GPT-4, the response is parsed from the Anthropic SDK (`message.content[0].text`).

### 1. Requirements

On Linux, `src/setup.sh` covers everything (it now also installs the `anthropic` SDK). On **macOS**, install the equivalents manually:

```bash
# Build toolchain
brew install maven
brew install --cask temurin@11        # JDK 11; JDK 8 e.g. via corretto or temurin@8
/usr/libexec/java_home -V              # verify both a 1.8 and an 11 JVM are listed

# Python dependencies (no torch/transformers needed for Claude or GPT-4 —
# they are imported lazily and only required for local HuggingFace models)
pip3 install anthropic beautifulsoup4 lxml
pip3 install "git+https://github.com/jose/javalang.git@start_position_and_end_position"
```

Notes:
- The `cmds/*.sh` scripts resolve `JAVA_HOME` via `/usr/libexec/java_home -v 1.8|11` on macOS and fall back to the original `/usr/lib/jvm/...` paths on Linux — no script editing needed on either platform.
- **Install a real JDK 11**: if `java_home -v 11` finds no JDK 11, macOS silently returns the newest installed JDK instead, which may build but change test behavior.
- On Apple Silicon everything runs natively; some old subject projects with native dependencies may still fail to build — pick another project from the dataset if so.

### 2. API key handling

Keep the key out of your shell history and out of the command line that shows up in `ps`:

```bash
# one-time: store the key in a user-readable-only file
echo "sk-ant-..." > ~/.anthropic_api_key && chmod 600 ~/.anthropic_api_key
```

Then always pass it by reading the file at invocation time: `--api-key "$(cat ~/.anthropic_api_key)"`. For `--model Claude` it carries the Anthropic key, which `flakydoctor.py` exports as `ANTHROPIC_API_KEY` for the run; the upstream flag name `--openai-key` still works as a deprecated alias. The key is never written into any output file.

### 3. Test-order caveat on stock Surefire

The original pipeline runs OD pairs with `-Dsurefire.runOrder=testorder`, which is **not a stock Surefire feature** — it comes from the Illinois research fork of Surefire used in the iDFlakies ecosystem. On a machine without that fork (any stock Maven, e.g. macOS/brew), stock Surefire aborts with `There's no RunOrder with the name testorder`.

Workaround built into `src/cmds/run_surefire.sh`: the run order is overridable via an environment variable (default remains `testorder`, so Linux setups with the fork are unaffected):

```bash
export SUREFIRE_RUN_ORDER=alphabetical        # or reversealphabetical
```

Stock run orders sort **classes**, so this only forces polluter-before-victim if the pair lives in two different classes whose names sort accordingly. Selecting a compatible pair from `datasets/OD_inputs.csv`:
- polluter class alphabetically **before** victim class → use `alphabetical` (e.g. `ConfigInjectionTest` → `TestCentralizedManagement` in light-4j)
- polluter class alphabetically **after** victim class → use `reversealphabetical`
- same-class pairs cannot be ordered this way (JUnit decides the method order) — they need the Surefire fork.

### 4. Worked end-to-end example (light-4j, fixed by Claude in 2 rounds)

```bash
cd FlakyDoctor

# (a) clone + build the subject project at its pinned SHA (~2 min)
echo "https://github.com/networknt/light-4j,fcded1683dcbd41a968e221494778aa6b71e7428,config" > /tmp/od_projects.csv
bash src/install.sh /tmp/od_projects.csv projects outputs install_summary.csv
cat install_summary.csv   # expect BUILD SUCCESS (jdk 8 fails by design, 11 succeeds)

# (b) one OD pair: victim, polluter (IDoFT format: url,sha,module,victim,polluter)
echo "https://github.com/networknt/light-4j,fcded1683dcbd41a968e221494778aa6b71e7428,config,com.networknt.config.TestCentralizedManagement.testMap_allowEmptyStringOverwrite,com.networknt.config.ConfigInjectionTest.testGetInjectValueIssue744" > /tmp/od_tests.csv

# (c) repair with Claude — use a FRESH output dir per run (see Notes)
export SUREFIRE_RUN_ORDER=alphabetical
python3 -u src/flakydoctor.py \
  --input-tests-csv /tmp/od_tests.csv \
  --flakiness-type OD \
  --projects projects \
  --api-key "$(cat ~/.anthropic_api_key)" \
  --model Claude \
  --output-dir outputs/claude_od_run1 \
  --output-result-csv outputs/claude_od_run1/results.csv \
  --output-result-json outputs/claude_od_run1/results.json \
  --output-details-json outputs/claude_od_run1/details.json
```

What you will see on stdout, in order: a jdk-8 surefire run failing with `UnsupportedClassVersionError` (expected — triggers the jdk-11 fallback), the jdk-11 run reproducing the flake (`Tests run: 2, Failures: 1`), then up to 5 repair rounds — each prints the full prompt sent to Claude, Claude's response, the applied patch, and the verification rerun. On `test_pass` the patch is saved and the loop stops.

### 5. Inspecting the results and the Claude conversation

```bash
cat outputs/claude_od_run1/results.csv               # one summary row per test
find outputs/claude_od_run1 -name "*.patch"          # verified patch (per round number)

# full round-by-round transcript of the Claude conversation
python3 - <<'EOF'
import json
raw = open('outputs/claude_od_run1/details.json').read()
obj, _ = json.JSONDecoder().raw_decode(raw, 0)   # first object = first run in this file
for r in sorted(obj['prompts'], key=int):
    print('='*70 + f'\nROUND {r} — PROMPT\n' + '='*70)
    print(obj['prompts'][r])
    print('-'*70 + f'\nROUND {r} — CLAUDE RESPONSE\n' + '-'*70)
    print(obj['responses'][r])
EOF
```

`details.json` also records, per round: the parsed patch before/after stitching, build/test results, and error messages — plus any API errors under `Exceptions` (an invalid key shows up there as an HTTP 401 `authentication_error`).

### 6. Notes

- **Use a fresh `--output-dir` per run.** The CSVs are overwritten on each run but the JSONs are appended and patch files mix across runs, so reusing a directory interleaves results.
- Re-running is cheap: subject projects stay built in `projects/`; a 2-round repair costs two Claude calls.
- Linux behavior with `--model GPT-4` or `MagiCoder` is unchanged by this fork (the MagicCoder path still requires local checkpoints and NVIDIA GPUs).
- LLM responses are non-deterministic: the fixing round and the patch itself vary between runs, and occasionally a test may not be fixed within the 5-round budget.

## 🌟 Pull requests
19 Tests have been accepted (one PR may include fixes for multiple tests):

**Accepted PRs:**
- https://github.com/funkygao/cp-ddd-framework/pull/65
- https://github.com/apache/pinot/pull/11771
- https://github.com/dropwizard/dropwizard/pull/7629
- https://github.com/opengoofy/hippo4j/pull/1495
- https://github.com/moquette-io/moquette/pull/781
- https://github.com/jnr/jnr-posix/pull/185
- https://github.com/FasterXML/jackson-jakarta-rs-providers/pull/22
- https://github.com/yangfuhai/jboot/pull/117

**Opened PRs:**
- https://github.com/perwendel/spark/pull/1285
- https://github.com/dyc87112/SpringBoot-Learning/pull/98
- https://github.com/graphhopper/graphhopper/pull/2899
- https://github.com/BroadleafCommerce/BroadleafCommerce/pull/2901
- https://github.com/dianping/cat/pull/2320
- https://github.com/hellokaton/30-seconds-of-java8/pull/8
- https://github.com/AmadeusITGroup/workflow-cps-global-lib-http-plugin/pull/68
- https://github.com/wro4j/wro4j/pull/1167
- https://github.com/kevinsawicki/http-request/pull/177
- https://github.com/apache/flink/pull/23648


*We are waiting for developers to approve our requests to create an issue for the following PRs:*
- https://github.com/dserfe/flink/pull/2
- https://github.com/dserfe/nifi/pull/1
- https://github.com/dserfe/jenkins/pull/1

**Why other tests can not be opened PRs:**
```
Tests are deleted in the latest version of the project:
- org.apache.dubbo.registry.client.metadata.ServiceInstanceMetadataUtilsTest.testMetadataServiceURLParameters
- org.apache.cayenne.CayenneContextClientChannelEventsIT.testSyncToOneRelationship
- org.apache.shardingsphere.elasticjob.cloud.scheduler.env.BootstrapEnvironmentTest.assertWithoutEventTraceRdbConfiguration
- org.apache.shardingsphere.elasticjob.cloud.scheduler.mesos.AppConstraintEvaluatorTest.assertExistExecutorOnS0
- net.sf.marineapi.ais.event.AbstractAISMessageListenerTest.testParametrizedConstructor
- net.sf.marineapi.ais.event.AbstractAISMessageListenerTest.testSequenceListener
- com.willwinder.universalgcodesender.GrblControllerTest.testGetGrblVersion
- com.willwinder.universalgcodesender.GrblControllerTest.testIsReadyToStreamFile

Tests are fixed by developers in the latest version of the project:
- io.elasticjob.lite.lifecycle.internal.settings.JobSettingsAPIImplTest.assertUpdateJobSettings
- net.sf.marineapi.ais.event.AbstractAISMessageListenerTest.testBasicListenerWithUnexpectedMessage
- net.sf.marineapi.ais.event.AbstractAISMessageListenerTest.testConstructor
- net.sf.marineapi.ais.event.AbstractAISMessageListenerTest.testGenericsListener
- net.sf.marineapi.ais.event.AbstractAISMessageListenerTest.testOnMessageWithExpectedMessage
- com.willwinder.universalgcodesender.GrblControllerTest.rawResponseHandlerOnErrorWithNoSentCommandsShouldSendMessageToConsole
- com.willwinder.universalgcodesender.GrblControllerTest.rawResponseHandlerWithKnownErrorShouldWriteMessageToConsole
- com.willwinder.universalgcodesender.GrblControllerTest.rawResponseHandlerWithUnknownErrorShouldWriteGenericMessageToConsole
- com.graphhopper.isochrone.algorithm.IsochroneTest.testSearch

Tests are actually different types of flakiness after inspection:
- com.baidu.jprotobuf.pbrpc.EchoServiceTest.testDynamiceTalkTimeout

Repository is archived:
- io.searchbox.indices.RolloverTest.testBasicUriGeneration
- com.netflix.exhibitor.core.config.zookeeper.TestZookeeperConfigProvider.testConcurrentModification
- org.springframework.security.oauth2.provider.client.JdbcClientDetailsServiceTests.testUpdateClientRedirectURI
``` 

