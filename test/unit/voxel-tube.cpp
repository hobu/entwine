#include "gtest/gtest.h"

#include <array>
#include <limits>
#include <map>
#include <random>

#include <entwine/builder/chunk.hpp>

using namespace entwine;

TEST(voxelTube, preservesVoxelsThroughInsertionAndGrowth)
{
    VoxelTube tube;
    std::map<uint32_t, std::array<char, 8>> data;
    std::mt19937 random(42);
    // Include both boundaries, noncontiguous keys, reverse insertion, and many
    // duplicate lookups. Stored point payloads must survive entry relocation.
    std::vector<uint32_t> keys{0, std::numeric_limits<uint32_t>::max(), 1};
    for (uint32_t i = 1024; i > 0; --i) keys.push_back(i);
    for (unsigned i = 0; i < 4096; ++i) keys.push_back(random() % 2048);
    for (const auto z : keys)
    {
        SpinGuard lock(tube.spin);
        auto& voxel = tube.getOrCreate(z);
        auto found = data.find(z);
        if (found == data.end())
        {
            EXPECT_EQ(voxel.data(), nullptr);
            auto& payload = data[z];
            std::array<char, 8> source;
            source.fill(static_cast<char>(z % 127));
            voxel.setData(payload.data());
            voxel.initDeep(Point(z, z + 0.25, z + 0.5), source.data(), source.size());
        }
        else
        {
            EXPECT_EQ(voxel.data(), found->second.data());
            EXPECT_EQ(voxel.point().x, z);
            EXPECT_EQ(voxel.point().y, z + 0.25);
            EXPECT_EQ(voxel.point().z, z + 0.5);
        }
    }
    for (const auto& entry : data)
    {
        SpinGuard lock(tube.spin);
        const auto& voxel = tube.getOrCreate(entry.first);
        EXPECT_EQ(voxel.data(), entry.second.data());
        EXPECT_EQ(voxel.point().x, entry.first);
        EXPECT_EQ(voxel.point().y, entry.first + 0.25);
        EXPECT_EQ(voxel.point().z, entry.first + 0.5);
        EXPECT_EQ(std::vector<char>(voxel.data(), voxel.data() + entry.second.size()),
            std::vector<char>(entry.second.size(), static_cast<char>(entry.first % 127)));
    }
}
