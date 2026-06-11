projectDir=$1
module=$2
polluterFormatTest=$3
victimFormatTest=$4
jdk=$5

mainDir=${projectDir}
curDir=$(pwd)

## runOrder=testorder requires the Illinois research fork of Surefire (iDFlakies ecosystem).
## On stock Surefire (e.g. macOS/brew setups), export SUREFIRE_RUN_ORDER=alphabetical and pick
## victim/polluter pairs whose polluter class sorts first alphabetically.
runOrder=${SUREFIRE_RUN_ORDER:-testorder}

run_test(){
    # NOTE: removed broken `--log-file ${logDir}/"$1".log` (logDir was never defined);
    # output must go to stdout because repair_OD.py parses the subprocess stdout.
    echo mvn test -pl ${module} -Dsurefire.runOrder=${runOrder} -Dtest=${polluterFormatTest},${victimFormatTest} -Drat.skip -Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true -Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Djacoco.skip -Dspotless.check.skip
    mvn test -pl ${module} -Dsurefire.runOrder=${runOrder} -Dtest=${polluterFormatTest},${victimFormatTest} -Drat.skip -Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true -Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Djacoco.skip -Dspotless.check.skip
}

echo "* RUNNING Surefire on OD tests ${polluterFormatTest} ${victimFormatTest} STARTING at $(date)"
echo "* REPO VERSION $(git rev-parse HEAD)"

cd ${mainDir}
echo "* CURRENT DIR $(pwd)"
echo "* Expected Java version ${jdk}"

if  [[ ${jdk} == "8" ]]; then
    echo "Java version 8"
    if [[ -x /usr/libexec/java_home ]]; then
        export JAVA_HOME=$(/usr/libexec/java_home -v 1.8)
    else
        export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk-amd64
    fi
    export PATH=$JAVA_HOME/bin:$PATH
fi

if  [[ ${jdk} == "11" ]]; then
    echo "Java version 11"
    if [[ -x /usr/libexec/java_home ]]; then
        export JAVA_HOME=$(/usr/libexec/java_home -v 11)
    else
        export JAVA_HOME=/usr/lib/jvm/java-1.11.0-openjdk-amd64
    fi
    export PATH=$JAVA_HOME/bin:$PATH
fi

echo CURRENT DIR $(pwd)

run_test ${i} ${polluterFormatTest} ${victimFormatTest}

cd ${curDir}
echo "* RUNNING Surefire on OD tests ${polluterFormatTest} ${victimFormatTest} ENDING at $(date)"