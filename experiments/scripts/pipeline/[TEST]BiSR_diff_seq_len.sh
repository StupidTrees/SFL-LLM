# 实验：跨层DRA攻击
seed=42

dataset_label='train'
exp_name='[TEST]Diff_seq_len'
client_num=1
global_round=1
client_steps=500
noise_scale=0.0
noise_mode="none"
attacker_prefix='normal'
data_shrink_frac=0.08
test_data_shrink_frac=0.3
evaluate_freq=1000
self_pt_enable=False
lora_at_trunk=True
lora_at_bottom=True
lora_at_top=True
collect_all_layers=True

model_name='llama2'
attack_model='gru'
sps='6-26'
attacker_sp=6
batch_size=2
dlg_enable=True
dlg_adjust=0
dlg_epochs=18
dlg_beta=0.85
dlg_lr=0.09
dlg_init_with_dra=True
dlg_raw_enable=True
dlg_method='tag'
attacker_freq=200
attacker_samples=10
max_global_step=610

attacker_datasets=("sensireplaced")
sfl_datasets=("dialogsum")
#("piqa" "codealpaca" "dialogsum"  "sensimarked" "gsm8k" "wikitext")
max_lengths=(32 64 128 256 512)

for attacker_dataset in "${attacker_datasets[@]}"; do
  for sfl_dataset in "${sfl_datasets[@]}"; do
    for max_length in "${max_lengths[@]}"; do
      if [ "$model_name" == "llama2" ]; then
        dlg_epochs=6
      fi

      # 先训练攻击模型
      echo "Running train_attacker.py with atk_ds=$attacker_dataset"
      python ../py/train_inverter.py \
        --model_name "$model_name" \
        --seed "$seed" \
        --dataset "$attacker_dataset" \
        --attack_model "$attack_model" \
        --attack_mode 'b2tr' \
        --noise_mode "$noise_mode" \
        --sps "$sps" \
        --dataset_test_frac 0.1 --save_checkpoint True \
        --log_to_wandb False

      case_name="${model_name}-${sfl_dataset}<${attacker_dataset}"

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
        --dataset_max_seq_len "$max_length" \
        --noise_scale_dxp "$noise_scale" \
        --exp_name "$exp_name" \
        --attacker_b2tr_sp "$attacker_sp" \
        --attacker_tr2t_sp "$attacker_sp" \
        --attacker_prefix "$attacker_prefix" \
        --self_pt_enable "$self_pt_enable" \
        --client_num "$client_num" \
        --data_shrink_frac "$data_shrink_frac" \
        --test_data_shrink_frac "$test_data_shrink_frac" \
        --evaluate_freq "$evaluate_freq" \
        --client_steps "$client_steps" \
        --lora_at_top "$lora_at_top" \
        --lora_at_trunk "$lora_at_trunk" \
        --lora_at_bottom "$lora_at_bottom" \
        --collect_all_layers "$collect_all_layers" \
        --dataset_label "$dataset_label" \
        --attacker_dataset "$attacker_dataset" \
        --batch_size "$batch_size" \
        --dlg_enable "$dlg_enable" \
        --dlg_adjust "$dlg_adjust" \
        --dlg_epochs "$dlg_epochs" \
        --dlg_beta "$dlg_beta" \
        --dlg_method "$dlg_method" \
        --dlg_lr "$dlg_lr" \
        --dlg_init_with_dra "$dlg_init_with_dra" \
        --dlg_raw_enable "$dlg_raw_enable" \
        --attacker_freq "$attacker_freq" \
        --attacker_samples "$attacker_samples" \
        --max_global_step "$max_global_step"
    done

  done
done
