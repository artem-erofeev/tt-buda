// SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
//
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <unordered_map>
#include <vector>

#include "balancer/legalizer/graph_solver.hpp"
#include "balancer/types.hpp"
#include "graph_lib/graph.hpp"
#include "graph_lib/node.hpp"

namespace tt::balancer
{
struct BalancerConfig;
}  // namespace tt::balancer

namespace tt::balancer
{
legalizer::GraphSolverSolution run_policy_maximize_t_minimize_grid(
    graphlib::Graph const* graph, BalancerConfig const&, legalizer::GraphSolver& graph_solver);

}  // namespace tt::balancer
