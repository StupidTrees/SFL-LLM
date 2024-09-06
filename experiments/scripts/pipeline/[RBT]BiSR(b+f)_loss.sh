# 实验：对Embedding Inversion Attack进行超参搜索
seed=42

dataset_label='train'
exp_name='[RBT]BiSR(b+f)_loss'
global_round=1
client_steps=500
noise_scale=0.0
noise_mode="none"
data_shrink_frac=0.08
test_data_shrink_frac=0.3
evaluate_freq=800
self_pt_enable=False
lora_at_trunk=True
lora_at_bottom=True
lora_at_top=True
collect_all_layers=True

sps="6-27"
batch_size=2

attacker_freq=100
attacker_samples=1
max_global_step=201

sip_inverter_dataset='sensireplaced'

model_name='llama2'
sfl_datasets=("piqa" "codealpaca" "dialogsum" "sensimarked" "gsm8k" "wikitext")

load_bits=8
cosine_similarity=(False True)

for dataset in "${sfl_datasets[@]}"; do
  sfl_dataset="$dataset"
  for cos_sim in "${cosine_similarity[@]}"; do

    case_name="BiSR(b+f)-pre@${model_name}@${sfl_dataset},${cos_sim}"

    if [ "$model_name" == "llama2" ]; then
      gma_lr=0.09
      gma_beta=0.85
      gma_epc=18
      gma_init_temp=1.2
      gsma_lr=0.005
      gsma_epc=800
      gsma_wd=0.02
    fi

    if [ "$model_name" == "gpt2-large" ]; then
      gma_lr=0.09
      gma_beta=0.85
      gma_epc=32
      gma_init_temp=1.2
      gsma_lr=0.01
      gsma_epc=800
      gsma_wd=0.01
    fi

    if [ "$model_name" == "chatglm" ]; then
      gma_lr=0.09
      gma_beta=0.85
      gma_epc=18
      gma_init_temp=1.2
      gsma_lr=0.005
      gsma_epc=800
      gsma_wd=0.01
    fi

    # 先训练攻击模型
    echo "Running train_inverter.py"
    python ../py/train_inverter.py \
      --model_name "$model_name" \
      --seed "$seed" \
      --attack_model "gru" \
      --dataset "$sip_inverter_dataset" \
      --attack_mode 'b2tr' \
      --sps "$sps" \
      --dataset_test_frac 0.1 \
      --save_checkpoint True \
      --log_to_wandb False --load_bits "$load_bits"

    # 将其用于攻击
    echo "Running evaluate_tag_methods.py with sfl_ds=$sfl_dataset"
    python ../py/sim_with_attacker.py \
      --noise_mode "$noise_mode" \
      --case_name "$case_name" \
      --model_name "$model_name" \
      --split_points "$sps" \
      --global_round "$global_round" \
      --seed "$seed" \
      --dataset "$sfl_dataset" \
      --noise_scale "$noise_scale" \
      --exp_name "$exp_name" \
      --self_pt_enable "$self_pt_enable" \
      --client_num 1 \
      --data_shrink_frac "$data_shrink_frac" \
      --test_data_shrink_frac "$test_data_shrink_frac" \
      --evaluate_freq "$evaluate_freq" \
      --client_steps "$client_steps" \
      --lora_at_top "$lora_at_top" \
      --lora_at_trunk "$lora_at_trunk" \
      --lora_at_bottom "$lora_at_bottom" \
      --collect_all_layers "$collect_all_layers" \
      --dataset_label "$dataset_label" \
      --batch_size "$batch_size" \
      --tag_enable False \
      --gma_enable True \
      --gsma_enable True \
      --sma_enable True \
      --eia_enable False \
      --attacker_freq "$attacker_freq" \
      --attacker_samples "$attacker_samples" \
      --max_global_step "$max_global_step" \
      --sip_dataset "$sip_inverter_dataset" \
      --sip_prefix "normal" \
      --sip_b2tr_enable True \
      --sip_b2tr_layer -1 \
      --sip_tr2t_enable False \
      --gma_lr "$gma_lr" \
      --gma_beta "$gma_beta" \
      --gma_epochs "$gma_epc" \
      --gma_init_temp "$gma_init_temp" \
      --gsma_cosine_loss "$cos_sim" \
      --gsma_lr "$gsma_lr" \
      --gsma_epochs "$gsma_epc" \
      --gsma_wd "$gsma_wd" \
      --sma_cosine_loss "$cos_sim" \
      --sma_lr "$gsma_lr" \
      --sma_epochs "$gsma_epc" \
      --sma_wd "$gsma_wd" \
      --load_bits "$load_bits"
  done
done
