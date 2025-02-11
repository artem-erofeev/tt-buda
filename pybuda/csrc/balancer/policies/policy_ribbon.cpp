// SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
//
// SPDX-License-Identifier: Apache-2.0
#include "balancer/policies/policy_ribbon.hpp"

#include "balancer/policies/policy_manager.hpp"

namespace tt::balancer
{
// Return true if all of node's sources have been scheduled already
/*
bool ok_to_schedule_next(
    const scheduler::Schedule &scheduled_ops, std::uint32_t scheduled_so_far, const Graph *graph, Node *node)
{
    for (Node *operand : graph->data_operands(node))
    {
        if (operand->node_type() != graphlib::kBudaOp)
            continue;

        auto it = std::find(scheduled_ops.begin(), scheduled_ops.end(), operand->name());
        std::cout << "ok to schedule? " << node->name() << ", operand: " << operand->name()
                  << ", delta: " << (it - scheduled_ops.begin()) << ", so far: " << scheduled_so_far << std::endl;
        if (it - scheduled_ops.begin() > scheduled_so_far)
            return false;
    }
    return true;
}
*/

legalizer::GraphSolverSolution run_policy_ribbon(
    graphlib::Graph const *graph,
    const BalancerConfig &config,
    legalizer::GraphSolver &graph_solver,
    std::optional<placer::PlacerSolution> &placer_solution)
{
    log_info(LogBalancer, "Starting Ribbon balancing.");
    PolicyManager policy_manager(graph, config, graph_solver, true /*ribbon_policy*/);
    if (env_as<bool>("PYBUDA_RIBBON1_PREPASS_ENABLED", false))
    {
    policy_manager.invalidate_suboptimal_op_models(legalizer::MatmulSparseDenseGridPairing | legalizer::DenseMatmulPrologue | legalizer::DenseMatmulBetterUkt);
    }

    bool epoch_completed = false;
    std::unordered_set<std::uint64_t> validated_cache;  // list of op model IDs that have been validated to be ok, so we
                                                        // don't have to validate them again
    const int target_cycles = env_as<int>("PYBUDA_RIBBON_TARGET_CYCLES", 45000);

    // Pick op models.
    //
    while (const graphlib::Node *node = policy_manager.get_next_op())
    {
        const graphlib::BudaOpNode *op = node->as<graphlib::BudaOpNode>();

        const auto &selected_op_model = select_best_op_model_ribbon(
            policy_manager,
            op,
            policy_manager.get_current_ribbon_size(),
            config,
            graph,
            validated_cache,
            target_cycles);

        std::tie(std::ignore, epoch_completed, std::ignore) = policy_manager.commit_op(selected_op_model);

        // If we're done with the epoch, finish it.
        //
        if (epoch_completed)
        {
            policy_manager.finish_current_epoch();
        }
    }

    placer_solution = policy_manager.commit_solution();

    return policy_manager.finish();
}

}  // namespace tt::balancer
