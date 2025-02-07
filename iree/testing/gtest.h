// Copyright 2019 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#ifndef IREE_TESTING_GTEST_H_
#define IREE_TESTING_GTEST_H_

#if defined(IREE_CONFIG_GOOGLE_INTERNAL) && IREE_CONFIG_GOOGLE_INTERNAL
#include "iree/testing/google_internal/gtest_google.h"
#else
#include "iree/testing/internal/gtest_internal.h"
#endif  // IREE_CONFIG_GOOGLE_INTERNAL

#endif  // IREE_TESTING_GTEST_H_
