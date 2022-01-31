#pragma once

//
// This file is distributed under the MIT License. See LICENSE.md for details.
//

#include "revng/Model/Binary.h"

namespace model {

void purgeUnnamedAndUnreachableTypes(TupleTree<model::Binary> &Model);

} // namespace model
