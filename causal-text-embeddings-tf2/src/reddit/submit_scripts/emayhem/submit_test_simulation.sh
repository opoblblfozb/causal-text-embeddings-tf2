#!/usr/bin/env bash

OUTPUT_DIR_BASE=/proj/sml_netapp/projects/causal-text-embeddings-tf2/tmp/
mkdir -p ${OUTPUT_DIR_BASE}
export MODE=train_only
export NUM_SPLITS=10
export SUBREDDITS=13,6,8
export SPLIT=0

#declare -a SIMMODES=('simple' 'multiplicative' 'interaction')
declare -a SIMMODES=('simple')

export BETA0=1.0
#todo: in place just to save time by avoiding repeating compute
#declare -a BETA1S=(1.0 5.0 25.0)
declare -a BETA1S=(10.0)
declare -a GAMMAS=(1.0)

for SIMMODEj in "${SIMMODES[@]}"; do
    export SIMMODE=${SIMMODEj}
    for BETA1j in "${BETA1S[@]}"; do
        export BETA1=${BETA1j}
        for GAMMAj in "${GAMMAS[@]}"; do
            export GAMMA=${GAMMAj}
            export OUTPUT_DIR=${OUTPUT_DIR_BASE}mode${SIMMODE}/beta0${BETA0}.beta1${BETA1}.gamma${GAMMA}/split${SPLIT}
            NAME=mode${SIMMODE}.beta0${BETA0}.beta1${BETA1}.gamma${GAMMA}.split${SPLIT}
            sbatch --job-name=subredditsim_${NAME} \
               --output=${OUTPUT_DIR_BASE}${NAME}.out \
               ./reddit/submit_scripts/emayhem/paper_experiments/run_subreddit.sh
        done
    done
done